"""Zip-member citability (fact-graph-over-Collections design §6/§7 follow-up):
each bundle-ingested member of a zip archive gets its OWN ``corpus_file_sources``
anchor, so a fact-graph claim can cite the exact member — not just the archive
— and so a routine re-sync of an N-member zip with one changed member keeps
the other N-1 members' rows, anchors and claims intact.

PG-only: ``corpus_file_sources`` and the facts tables are PG-only app-state
(A3 ratchet) — no DuckDB half to parametrize against. ``tests/test_ingest_bundle.py``
covers the DuckDB-backend half of ``src.ingest.bundle`` (anchors silently
no-op there); the zip-safety guards (zip-slip, nested archive, member/size
caps) are backend-agnostic and already covered there too — the guard test
below only proves the anchor-writing change didn't disturb them.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _setup_pg(pg_engine, monkeypatch, tmp_path):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    import src.repositories as factory

    return factory


@pytest.fixture
def pg_repos(pg_engine, monkeypatch, tmp_path):
    return _setup_pg(pg_engine, monkeypatch, tmp_path)


def _new_corpus(factory, slug: str) -> str:
    return factory.file_corpora_repo().create(name=slug, slug=slug, description=None, created_by="u1")


def _store(corpus_id: str, filename: str, data: bytes):
    from src.file_storage import store_corpus_bytes

    return store_corpus_bytes(corpus_id, filename, data)


def _fake_index_with_chunk(child_id: str) -> str:
    """Ingest fake that mirrors real Tier-1 text ingestion closely enough for
    the verbatim gate: status -> indexed, one chunk holding the member's own
    stored bytes (so a claim quoting the member's CONTENT resolves through
    the normal chunk path, not the identity-haystack fallback)."""
    from src.repositories import corpus_chunks_repo, corpus_files_repo

    cf_repo = corpus_files_repo()
    row = cf_repo.get(child_id)
    text = ""
    if row.get("storage_path"):
        with open(row["storage_path"], "rb") as fh:
            text = fh.read().decode("utf-8", errors="ignore")
    corpus_chunks_repo().add_many([{"corpus_id": row["corpus_id"], "file_id": child_id, "ordinal": 0, "text": text}])
    cf_repo.set_status(child_id, status="indexed", detail={})
    return "indexed"


def _fake_index_no_chunk(child_id: str) -> str:
    """Ingest fake for the identity-haystack test: indexed, but with NO
    chunks — so any quote can only be accepted via the member's own
    filename/path, never its content."""
    from src.repositories import corpus_files_repo

    corpus_files_repo().set_status(child_id, status="indexed", detail={})
    return "indexed"


def _claim_count(engine, file_id: str) -> int:
    with engine.begin() as conn:
        return conn.execute(
            sa.text("SELECT count(*) FROM claims WHERE corpus_file_id = :f"), {"f": file_id}
        ).scalar_one()


def _seed_claim(repo, *, file_id: str, corpus_id: str, natural_key: str, sha: str, quote: str) -> str:
    fact_id = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=fact_id, type="engagement", natural_key=natural_key)
    repo.add_claim(fact_id=fact_id, corpus_file_id=file_id, corpus_id=corpus_id, file_sha256=sha, quote=quote)
    return fact_id


def _members_by_filename(factory, archive_id: str) -> dict[str, dict]:
    return {k["filename"]: k for k in factory.corpus_files_repo().list_children(archive_id)}


# ---------------------------------------------------------------------------
# 1. Member anchors are resolvable by their own doc_id, and a claim citing a
#    member attaches to the MEMBER row and inherits the collection's
#    visibility (a claim's `corpus_id` is the collection it was extracted
#    from — the join RBAC filters on, spec §2/§3).
# ---------------------------------------------------------------------------


def test_three_member_zip_gets_three_member_anchors_resolvable_by_doc_id(pg_repos, pg_engine):
    from src.ingest.bundle import ingest_bundle

    corpus_id = _new_corpus(pg_repos, "anchors-3")
    data = _zip_bytes({"a.md": b"alpha content", "b.md": b"beta content", "c.md": b"gamma content"})
    stored = _store(corpus_id, "dump.zip", data)
    archive_id = pg_repos.corpus_files_repo().add(
        corpus_id=corpus_id,
        filename="dump.zip",
        sha256=stored.sha256,
        file_type="zip",
        size_bytes=stored.size_bytes,
        storage_path=stored.storage_path,
    )

    status = ingest_bundle(corpus_id, archive_id, stored.storage_path, ingest_child=_fake_index_with_chunk)
    assert status == "indexed"

    sources_repo = pg_repos.corpus_file_sources_repo()
    members = _members_by_filename(pg_repos, archive_id)
    assert set(members) == {"a.md", "b.md", "c.md"}

    for name, member in members.items():
        stable_id = f"{archive_id}!{name}"
        assert sources_repo.resolve(corpus_id, stable_id) == member["id"]
        doc_id = member["sha256"][:16]
        mapping = sources_repo.get_by_source_doc_id(doc_id)
        assert mapping is not None
        assert mapping["corpus_file_id"] == member["id"]

    # A claim citing ONE member (by its own doc_id, no `documents[]` entry
    # needed — the anchor was already written at bundle-ingest time) attaches
    # to that member's row, not the archive's.
    facts_repo = pg_repos.facts_repo()
    b_member = members["b.md"]
    result = facts_repo.ingest_batch(
        nodes=[
            {
                "id": "engagement:proj-b",
                "type": "engagement",
                "evidence": [{"doc_id": b_member["sha256"][:16], "quote": "beta content"}],
            }
        ]
    )
    assert result["claims_written"] == 1, result
    assert result["claims_rejected"] == []

    with pg_engine.begin() as conn:
        claim = (
            conn.execute(
                sa.text("SELECT corpus_file_id, corpus_id FROM claims WHERE quote = :q"), {"q": "beta content"}
            )
            .mappings()
            .one()
        )
    assert claim["corpus_file_id"] == b_member["id"], "the claim must attach to the MEMBER, not the archive"
    assert claim["corpus_file_id"] != archive_id
    assert claim["corpus_id"] == corpus_id, "the claim inherits the collection's visibility (corpus_id)"


# ---------------------------------------------------------------------------
# 2. THE LOAD-BEARING TEST: a routine re-upload of the archive with ONE
#    member's content changed must purge only that member's claims — the
#    other members keep their corpus_file ids AND their claims.
# ---------------------------------------------------------------------------


def test_reupload_with_one_member_changed_purges_only_that_members_claims(pg_repos, pg_engine):
    from app.api.collections import _upsert_corpus_file
    from src.ingest.bundle import ingest_bundle

    corpus_id = _new_corpus(pg_repos, "reupload-3")
    sources_repo = pg_repos.corpus_file_sources_repo()
    cf_repo = pg_repos.corpus_files_repo()
    facts_repo = pg_repos.facts_repo()

    v1 = _zip_bytes({"a.md": b"alpha v1", "b.md": b"beta v1", "c.md": b"gamma v1"})
    stored_v1 = _store(corpus_id, "dump.zip", v1)

    archive_id, needs_processing, _ = _upsert_corpus_file(
        corpus_id,
        path=None,
        stable_id="graph:dumpzip",
        source_doc_id=None,
        source_sha256_meta=None,
        filename="dump.zip",
        sha256=stored_v1.sha256,
        file_type="zip",
        size_bytes=stored_v1.size_bytes,
        storage_path=stored_v1.storage_path,
        sources_repo=sources_repo,
    )
    assert needs_processing is True
    assert (
        ingest_bundle(corpus_id, archive_id, stored_v1.storage_path, ingest_child=_fake_index_with_chunk) == "indexed"
    )

    members_v1 = _members_by_filename(pg_repos, archive_id)
    claims_by_name = {}
    for name, member in members_v1.items():
        claims_by_name[name] = _seed_claim(
            facts_repo,
            file_id=member["id"],
            corpus_id=corpus_id,
            natural_key=f"engagement:{name}",
            sha=member["sha256"],
            quote=f"seeded claim for {name}",
        )
    assert _claim_count(pg_engine, members_v1["a.md"]["id"]) == 1
    assert _claim_count(pg_engine, members_v1["b.md"]["id"]) == 1
    assert _claim_count(pg_engine, members_v1["c.md"]["id"]) == 1

    # Re-upload: a.md and b.md byte-identical, c.md's content changed — so the
    # ARCHIVE's own sha256 changes too (routine re-sync of a mostly-unchanged
    # zip).
    v2 = _zip_bytes({"a.md": b"alpha v1", "b.md": b"beta v1", "c.md": b"gamma v2 CHANGED"})
    stored_v2 = _store(corpus_id, "dump.zip", v2)
    assert stored_v2.sha256 != stored_v1.sha256

    archive_id2, needs_processing2, claims_purged = _upsert_corpus_file(
        corpus_id,
        path=None,
        stable_id="graph:dumpzip",
        source_doc_id=None,
        source_sha256_meta=None,
        filename="dump.zip",
        sha256=stored_v2.sha256,
        file_type="zip",
        size_bytes=stored_v2.size_bytes,
        storage_path=stored_v2.storage_path,
        sources_repo=sources_repo,
    )
    assert archive_id2 == archive_id, "the archive row's own id must be preserved (§6)"
    assert needs_processing2 is True
    assert claims_purged == 0, "the ARCHIVE row itself never carries claims — only its members do"

    # The archive row itself carries no chunks/claims, so nothing about it
    # purging its own (nonexistent) claims proves member stability yet — the
    # real assertion is what `ingest_bundle`'s reconciliation does next,
    # exactly as the background task would run it.
    assert (
        ingest_bundle(corpus_id, archive_id, stored_v2.storage_path, ingest_child=_fake_index_with_chunk) == "indexed"
    )

    members_v2 = _members_by_filename(pg_repos, archive_id)
    assert set(members_v2) == {"a.md", "b.md", "c.md"}

    # a.md and b.md: SAME corpus_file id, SAME claim still there.
    assert members_v2["a.md"]["id"] == members_v1["a.md"]["id"]
    assert members_v2["b.md"]["id"] == members_v1["b.md"]["id"]
    assert _claim_count(pg_engine, members_v1["a.md"]["id"]) == 1, "unrelated member's claim must survive the re-sync"
    assert _claim_count(pg_engine, members_v1["b.md"]["id"]) == 1, "unrelated member's claim must survive the re-sync"

    # c.md: content changed -> a NEW row (matches the pre-existing
    # (filename, sha256) child-matching contract, unaffected by this change).
    # Its OLD id is gone, along with its old claim; the OLD anchor is gone too
    # (FK cascade), and a FRESH anchor exists for the new content.
    old_c_id = members_v1["c.md"]["id"]
    new_c_id = members_v2["c.md"]["id"]
    assert new_c_id != old_c_id
    assert cf_repo.get(old_c_id) is None, "the changed member's OLD row must be hard-deleted"
    assert _claim_count(pg_engine, old_c_id) == 0
    assert _claim_count(pg_engine, new_c_id) == 0, "a freshly re-minted row starts with no claims of its own"
    assert sources_repo.resolve(corpus_id, f"{archive_id}!c.md") == new_c_id
    assert sources_repo.get_by_source_doc_id(members_v2["c.md"]["sha256"][:16])["corpus_file_id"] == new_c_id
    # The old anchor cannot still resolve to a row that no longer exists.
    assert sources_repo.get(old_c_id) is None


# ---------------------------------------------------------------------------
# 3. Member rename: identity is keyed on (filename, sha256) — unchanged by
#    this task — so a rename is delete-old + create-new, not an in-place
#    move. Documented behavior, not a bug: the archive-level upsert (§6) is
#    what preserves identity across a rename; the bundle-member matching
#    contract inside one archive was never asked to.
# ---------------------------------------------------------------------------


def test_member_rename_is_delete_old_and_create_new_not_an_in_place_move(pg_repos, pg_engine):
    from app.api.collections import _upsert_corpus_file
    from src.ingest.bundle import ingest_bundle

    corpus_id = _new_corpus(pg_repos, "rename-1")
    sources_repo = pg_repos.corpus_file_sources_repo()
    cf_repo = pg_repos.corpus_files_repo()
    facts_repo = pg_repos.facts_repo()

    v1 = _zip_bytes({"a.md": b"same bytes"})
    stored_v1 = _store(corpus_id, "dump.zip", v1)
    archive_id, _, _ = _upsert_corpus_file(
        corpus_id,
        path=None,
        stable_id="graph:renamezip",
        source_doc_id=None,
        source_sha256_meta=None,
        filename="dump.zip",
        sha256=stored_v1.sha256,
        file_type="zip",
        size_bytes=stored_v1.size_bytes,
        storage_path=stored_v1.storage_path,
        sources_repo=sources_repo,
    )
    ingest_bundle(corpus_id, archive_id, stored_v1.storage_path, ingest_child=_fake_index_with_chunk)
    old_member = _members_by_filename(pg_repos, archive_id)["a.md"]
    _seed_claim(
        facts_repo,
        file_id=old_member["id"],
        corpus_id=corpus_id,
        natural_key="engagement:renamed",
        sha=old_member["sha256"],
        quote="seeded before rename",
    )
    assert _claim_count(pg_engine, old_member["id"]) == 1

    # Same content, renamed member inside the archive -> the ARCHIVE's own
    # bytes (and sha256) still change, because the zip's central directory
    # entry name changed.
    v2 = _zip_bytes({"renamed.md": b"same bytes"})
    stored_v2 = _store(corpus_id, "dump.zip", v2)
    assert stored_v2.sha256 != stored_v1.sha256

    archive_id2, _, _ = _upsert_corpus_file(
        corpus_id,
        path=None,
        stable_id="graph:renamezip",
        source_doc_id=None,
        source_sha256_meta=None,
        filename="dump.zip",
        sha256=stored_v2.sha256,
        file_type="zip",
        size_bytes=stored_v2.size_bytes,
        storage_path=stored_v2.storage_path,
        sources_repo=sources_repo,
    )
    assert archive_id2 == archive_id
    ingest_bundle(corpus_id, archive_id, stored_v2.storage_path, ingest_child=_fake_index_with_chunk)

    members_v2 = _members_by_filename(pg_repos, archive_id)
    assert set(members_v2) == {"renamed.md"}
    new_member = members_v2["renamed.md"]

    assert new_member["id"] != old_member["id"], "documented: a rename mints a NEW member row"
    assert cf_repo.get(old_member["id"]) is None
    assert _claim_count(pg_engine, old_member["id"]) == 0, "the old row's claim is gone with the row"
    assert sources_repo.get(old_member["id"]) is None, "the old anchor is gone (FK cascade)"
    assert sources_repo.resolve(corpus_id, f"{archive_id}!renamed.md") == new_member["id"]


# ---------------------------------------------------------------------------
# 4. Existing zip guards (zip-slip, nested archive, size caps) still hold —
#    and a rejected member never gets an anchor (it has no real content sha).
# ---------------------------------------------------------------------------


def test_guards_still_hold_and_rejected_members_get_no_anchor(pg_repos):
    import src.ingest.bundle as bundle

    corpus_id = _new_corpus(pg_repos, "guards-1")
    sources_repo = pg_repos.corpus_file_sources_repo()

    data = _zip_bytes(
        {
            "../escape.txt": b"zip-slip attempt",
            "inner.zip": b"PK\x03\x04fakezip",
            "ok.md": b"fine content",
        }
    )
    stored = _store(corpus_id, "dump.zip", data)
    archive_id = pg_repos.corpus_files_repo().add(
        corpus_id=corpus_id,
        filename="dump.zip",
        sha256=stored.sha256,
        file_type="zip",
        size_bytes=stored.size_bytes,
        storage_path=stored.storage_path,
    )
    assert bundle.ingest_bundle(corpus_id, archive_id, stored.storage_path, ingest_child=_fake_index_with_chunk) == (
        "indexed"
    )

    members = _members_by_filename(pg_repos, archive_id)
    assert members["../escape.txt"]["processing_status"] == "rejected"
    assert members["../escape.txt"]["processing_detail"]["reason"] == "unsafe_path"
    assert members["inner.zip"]["processing_status"] == "rejected"
    assert members["inner.zip"]["processing_detail"]["reason"] == "nested_archive_unsupported"
    assert members["ok.md"]["processing_status"] == "indexed"

    # Guards intact AND no anchor for a member that never had real content.
    assert sources_repo.get(members["../escape.txt"]["id"]) is None
    assert sources_repo.get(members["inner.zip"]["id"]) is None
    assert sources_repo.get(members["ok.md"]["id"]) is not None

    # Member count cap still holds on the PG backend too.
    over = _zip_bytes({"a.txt": b"a", "b.txt": b"b"})
    stored_over = _store(corpus_id, "limits.zip", over)
    limits_id = pg_repos.corpus_files_repo().add(
        corpus_id=corpus_id,
        filename="limits.zip",
        sha256=stored_over.sha256,
        file_type="zip",
        size_bytes=stored_over.size_bytes,
        storage_path=stored_over.storage_path,
    )
    orig_max = bundle.MAX_BUNDLE_MEMBERS
    bundle.MAX_BUNDLE_MEMBERS = 1
    try:
        assert bundle.ingest_bundle(corpus_id, limits_id, stored_over.storage_path) == "rejected"
    finally:
        bundle.MAX_BUNDLE_MEMBERS = orig_max
    assert pg_repos.corpus_files_repo().get(limits_id)["processing_detail"]["reason"] == "too_many_members"


# ---------------------------------------------------------------------------
# 5. Cross-check with #1767's verbatim-gate widening: a quote grounded in a
#    MEMBER's own stored filename/path is accepted for the MEMBER, never
#    (mistakenly) the archive.
# ---------------------------------------------------------------------------


def test_identity_grounded_quote_accepted_for_the_member_not_the_archive(pg_repos, pg_engine):
    from src.ingest.bundle import ingest_bundle

    corpus_id = _new_corpus(pg_repos, "identity-1")
    data = _zip_bytes({"Project Kemp/Overview.md": b"body text unrelated to the filename"})
    stored = _store(corpus_id, "dump.zip", data)
    archive_id = pg_repos.corpus_files_repo().add(
        corpus_id=corpus_id,
        filename="dump.zip",
        sha256=stored.sha256,
        file_type="zip",
        size_bytes=stored.size_bytes,
        storage_path=stored.storage_path,
    )
    # No chunks written -> the CONTENT-based gate can never match; only the
    # member's own identity haystack (filename/path) can ground the quote.
    assert ingest_bundle(corpus_id, archive_id, stored.storage_path, ingest_child=_fake_index_no_chunk) == "indexed"

    member = _members_by_filename(pg_repos, archive_id)["Project Kemp/Overview.md"]

    facts_repo = pg_repos.facts_repo()
    result = facts_repo.ingest_batch(
        nodes=[
            {
                "id": "fact:project-kemp",
                "type": "engagement",
                "evidence": [{"doc_id": member["sha256"][:16], "quote": "Project Kemp"}],
            }
        ]
    )
    assert result["claims_written"] == 1, result
    assert result["claims_accepted_via_identity"] == 1

    with pg_engine.begin() as conn:
        claim = (
            conn.execute(sa.text("SELECT corpus_file_id FROM claims WHERE quote = :q"), {"q": "Project Kemp"})
            .mappings()
            .one()
        )
    assert claim["corpus_file_id"] == member["id"], "identity-grounded quote must attach to the MEMBER"
    assert claim["corpus_file_id"] != archive_id, "the archive's OWN filename ('dump.zip') never contains this quote"
