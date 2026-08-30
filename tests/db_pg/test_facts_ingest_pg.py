"""Write-path tests for the fact graph over Collections (build order step 4
of docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md).

PG-only, no DuckDB half to parametrize against (A3 ratchet) — see
``docs/migrations.md`` -> "Adding a PG-only feature". Two layers:

* Direct :class:`FactsPgRepository` calls, with ``corpus_files``/
  ``corpus_chunks`` seeded through raw SQL (mirroring the seeding style
  ``tests/db_pg/test_facts_read_pg.py`` uses for facts/claims) — precise,
  fast, and the bulk of this file.
* HTTP round-trips via ``build_seeded_client("pg", ...)`` (the bottom
  section) — batch caps, corrections CRUD, the real upload -> ingest ->
  search end-to-end path, and the collections-delete sweep hook, proving
  the endpoint wiring itself. ``tests/test_api_facts_ingest.py`` covers the
  DuckDB-backend auth/flag/validation-shape half of the same routes (no
  ``pg_engine`` fixture reachable outside ``tests/db_pg/``).

Every id below is the literal acceptance test named in the spec
(§15.2 C-tests, §15.3 EQ-tests); docstrings restate the failure mode.
"""

from __future__ import annotations

import io
import secrets
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_A = "col_a"


# ---------------------------------------------------------------------------
# fixtures / seeding helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_env(tmp_path, monkeypatch, pg_engine):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from tests.db_pg._parity_sweep_util import _seed_pg_system_groups

    _seed_pg_system_groups(pg_engine)

    # `_admin()` below is a REAL Admin-group member, not a bare dict shape —
    # `accessible_collection_ids` resolves admin status via an actual DB
    # membership lookup (src.rbac.get_accessible_ids), so a fabricated
    # {"id": "admin1"} with no membership row is an ordinary ungranted user
    # and every search()/claims() call would silently see nothing.
    from src.repositories import user_group_members_repo, users_repo

    users_repo().create(id="admin1", email="admin@test.com", name="Admin")
    with pg_engine.connect() as conn:
        admin_gid = conn.execute(sa.text("SELECT id FROM user_groups WHERE name = 'Admin'")).scalar()
    user_group_members_repo().add_member("admin1", admin_gid, source="system_seed")
    return pg_engine


@pytest.fixture
def repo(pg_env):
    from src.repositories.facts_pg import FactsPgRepository

    import src.db_pg as db_pg

    return FactsPgRepository(db_pg.get_engine())


def _seed_collection(*, collection_id: str, created_by: str = "uploader1") -> str:
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :slug, :name, :by)"),
            {"id": collection_id, "slug": collection_id, "name": collection_id, "by": created_by},
        )
    return collection_id


def _seed_corpus_file(
    *,
    corpus_id: str = CORPUS_A,
    file_id: str,
    sha256: str = "sha1",
    status: str = "indexed",
    path: str | None = None,
    filename: str | None = None,
) -> None:
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO corpus_files (id, corpus_id, filename, sha256, processing_status, path) "
                "VALUES (:id, :corpus_id, :filename, :sha256, :status, :path)"
            ),
            {
                "id": file_id,
                "corpus_id": corpus_id,
                "filename": filename or f"{file_id}.md",
                "sha256": sha256,
                "status": status,
                "path": path,
            },
        )


def _seed_chunk(*, corpus_id: str = CORPUS_A, file_id: str, text: str, ordinal: int = 0) -> None:
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO corpus_chunks (id, corpus_id, file_id, ordinal, text) "
                "VALUES (:id, :corpus_id, :file_id, :ordinal, :text)"
            ),
            {
                "id": "ck_" + secrets.token_hex(8),
                "corpus_id": corpus_id,
                "file_id": file_id,
                "ordinal": ordinal,
                "text": text,
            },
        )


def _seed_source_mapping(
    *, corpus_id: str = CORPUS_A, file_id: str, source_doc_id: str, stable_id: str | None = None
) -> None:
    from src.repositories import corpus_file_sources_repo

    corpus_file_sources_repo().upsert(
        corpus_file_id=file_id,
        corpus_id=corpus_id,
        source_stable_id=stable_id or file_id,
        source_doc_id=source_doc_id,
    )


def _seed_ready_doc(
    repo_engine,
    *,
    file_id: str = "cf_a1",
    doc_id: str = "doc1",
    text: str = "The engagement is underway and on schedule.",
) -> str:
    """One indexed corpus_file with one chunk, mapped to ``doc_id`` — the
    minimal fixture most write-path tests build on."""
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id=file_id)
    _seed_chunk(file_id=file_id, text=text)
    _seed_source_mapping(file_id=file_id, source_doc_id=doc_id)
    return doc_id


# ---------------------------------------------------------------------------
# EQ1 — the verbatim gate rejects fabrication, non-zero rejection count.
# ---------------------------------------------------------------------------


def test_eq1_verbatim_gate_rejects_a_fabricated_quote(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env)
    report = repo.ingest_batch(
        nodes=[
            {
                "id": "engagement:acme-rollout",
                "type": "engagement",
                "attrs": {},
                "evidence": [{"doc_id": doc_id, "quote": "This sentence was never in the document."}],
            }
        ]
    )
    assert report["claims_written"] == 0
    assert len(report["claims_rejected"]) == 1
    assert report["claims_rejected"][0]["reason"] == "verbatim_gate_failed"
    assert report["subjects_created"] == 1  # the node itself still resolves/creates


def test_verbatim_gate_accepts_a_real_substring(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env)
    report = repo.ingest_batch(
        nodes=[
            {
                "id": "engagement:acme-rollout",
                "type": "engagement",
                "attrs": {},
                "evidence": [{"doc_id": doc_id, "quote": "engagement is underway"}],
            }
        ]
    )
    assert report["claims_written"] == 1
    assert report["claims_rejected"] == []


# ---------------------------------------------------------------------------
# Identity haystack — a document's own SERVER-STORED filename/path counts as
# verbatim evidence too (spec §8, live regression: the extraction ontology
# legitimately grounds e.g. a `part_of` edge in the document's folder path +
# filename, which the chunk-only gate rejected).
# ---------------------------------------------------------------------------


def test_verbatim_gate_accepts_a_quote_grounded_in_the_stored_filename(pg_env, repo):
    """A `part_of` edge citing the document's FULL folder + filename (a
    contiguous run of whole path components) counts as verbatim evidence —
    the exact scenario PR #1767 widened the gate for."""
    file_id = "cf_identity1"
    doc_id = "doc_identity1"
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(
        file_id=file_id,
        filename="Parts Authority — Overview.pptx",
        path="Project Kemp/Parts Authority — Overview.pptx",
    )
    _seed_chunk(file_id=file_id, text="Nothing about the filename appears in the extracted text.")
    _seed_source_mapping(file_id=file_id, source_doc_id=doc_id)

    report = repo.ingest_batch(
        nodes=[{"id": "engagement:kemp", "type": "engagement", "attrs": {}, "evidence": []}],
        edges=[
            {
                "type": "part_of",
                "src": "engagement:kemp",
                "dst": "project:kemp",
                "evidence": [{"doc_id": doc_id, "quote": "Project Kemp/Parts Authority — Overview.pptx"}],
            }
        ],
    )
    assert report["claims_written"] == 1
    assert report["claims_rejected"] == []
    assert report["claims_accepted_via_identity"] == 1


# ---------------------------------------------------------------------------
# P0 review finding: the identity gate must match a WHOLE unit (a folder
# name, the filename with/without its extension, or a contiguous run of
# whole path components) — never an arbitrary substring. `quote in path`
# admitted `.pptx`, `/`, or any fragment, letting a fabricated attribute
# self-certify as a cited quote via the document's own identity.
# ---------------------------------------------------------------------------


def _seed_identity_doc(file_id: str = "cf_identity_unit", doc_id: str = "doc_identity_unit") -> str:
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(
        file_id=file_id,
        filename="Parts Authority — Overview.pptx",
        path="Project Kemp/Parts Authority — Overview.pptx",
    )
    _seed_chunk(file_id=file_id, text="Nothing about the filename appears in the extracted text.")
    _seed_source_mapping(file_id=file_id, source_doc_id=doc_id)
    return doc_id


def _identity_edge_report(repo, doc_id: str, quote: str) -> dict:
    return repo.ingest_batch(
        nodes=[{"id": "engagement:kemp2", "type": "engagement", "attrs": {}, "evidence": []}],
        edges=[
            {
                "type": "part_of",
                "src": "engagement:kemp2",
                "dst": "project:kemp2",
                "evidence": [{"doc_id": doc_id, "quote": quote}],
            }
        ],
    )


@pytest.mark.parametrize(
    "quote",
    [
        "Parts Authority — Overview.pptx",  # full filename, with extension
        "Parts Authority — Overview",  # full filename, without extension
        "Project Kemp",  # a whole folder name
        "Project Kemp/Parts Authority — Overview.pptx",  # folder + filename
    ],
)
def test_verbatim_gate_accepts_whole_identity_units(pg_env, repo, quote):
    doc_id = _seed_identity_doc()
    report = _identity_edge_report(repo, doc_id, quote)
    assert report["claims_written"] == 1
    assert report["claims_rejected"] == []
    assert report["claims_accepted_via_identity"] == 1


@pytest.mark.parametrize(
    "quote,reason",
    [
        # Degenerate SHAPES — the meaningfulness floor runs ahead of both halves
        # of the gate, so these never reach the identity comparison at all and
        # carry its own reason.
        (".pptx", "quote_not_meaningful"),  # bare extension: starts with punctuation
        ("/", "quote_not_meaningful"),  # bare path separator
        # Meaningful shapes that are nonetheless only FRAGMENTS of the identity.
        # These pass the floor and DO reach the identity gate, which is what this
        # test exists to prove — if every case here were degenerate, the identity
        # gate would silently stop being exercised.
        ("ho", "verbatim_gate_failed"),  # 2-char fragment of "Authority", absent from the chunk text
        ("rts Authority — Overvi", "verbatim_gate_failed"),  # mid-word slice of the filename
    ],
)
def test_verbatim_gate_rejects_partial_identity_fragments(pg_env, repo, quote, reason):
    doc_id = _seed_identity_doc()
    report = _identity_edge_report(repo, doc_id, quote)
    assert report["claims_written"] == 0
    assert report["claims_rejected"][0]["reason"] == reason
    assert report["claims_accepted_via_identity"] == 0


def test_verbatim_gate_counter_only_counts_identity_accepted_claims(pg_env, repo):
    """A batch mixing a chunk-grounded claim, an identity-grounded claim,
    and a fabricated one: `claims_accepted_via_identity` must count ONLY
    the identity one."""
    doc_id = _seed_identity_doc(file_id="cf_identity_mixed", doc_id="doc_identity_mixed")
    report = repo.ingest_batch(
        nodes=[
            {
                "id": "engagement:mixed",
                "type": "engagement",
                "attrs": {},
                "evidence": [
                    {"doc_id": doc_id, "quote": "Nothing about the filename appears"},  # chunk-grounded
                    {"doc_id": doc_id, "quote": "Parts Authority — Overview.pptx"},  # identity-grounded
                    # A MEANINGFUL fabrication, not a degenerate one: it has to
                    # reach the identity gate for this test to prove anything
                    # about the identity counter. A bare ".pptx" is now stopped
                    # by the meaningfulness floor before it ever gets there.
                    {"doc_id": doc_id, "quote": "Overview"},  # fabricated fragment
                ],
            }
        ],
    )
    assert report["claims_written"] == 2
    assert len(report["claims_rejected"]) == 1
    assert report["claims_rejected"][0]["reason"] == "verbatim_gate_failed"
    assert report["claims_accepted_via_identity"] == 1


# ---------------------------------------------------------------------------
# Cross-PR regression (caught by merging with #1773, zip-member citability):
# a zip member's stored `filename` IS itself a path
# (`src/ingest/bundle.py::cf_repo.add(..., filename=member_path)`, no
# `path=` at all) — `_identity_candidates` must decompose `filename` the
# SAME way it decomposes `path`, not only `path`. Direct unit tests on the
# function (no DB), plus one ingest-level reproduction of the exact shape
# a bundle member's `corpus_files` row has.
# ---------------------------------------------------------------------------


def test_identity_candidates_decomposes_a_separator_bearing_filename():
    from src.repositories.facts_pg import _identity_candidates

    candidates = _identity_candidates("Project Kemp/Overview.pptx", None)
    assert "Project Kemp" in candidates  # a whole path component
    assert "Project Kemp/Overview.pptx" in candidates  # the full string
    assert "Project Kemp/Overview" in candidates  # the stem
    # finding 1's tightening must still hold when the filename has a "/":
    assert ".pptx" not in candidates
    assert "/" not in candidates
    assert "ve" not in candidates  # 2-char slice


def test_identity_candidates_ordinary_filename_is_unaffected():
    """A separator-free `filename` (the pre-bundle-support, ordinary case)
    must decompose to EXACTLY `{filename, stem}` — proves the bundle fix
    changed nothing for it, and that a bare extension is still rejected."""
    from src.repositories.facts_pg import _identity_candidates

    assert _identity_candidates("deck.pptx", None) == {"deck.pptx", "deck"}


def test_verbatim_gate_accepts_a_component_of_a_separator_bearing_filename(pg_env, repo):
    """Reproduces the actual #1773 cross-PR failure at the repo level,
    without cherry-picking `src/ingest/bundle.py`: a `corpus_files` row
    whose `filename` is itself an archive-relative path (a zip member,
    `path` NULL) must still ground an identity claim on ANY of its whole
    path components, not just the filename as one indivisible unit."""
    file_id = "cf_zip_member"
    doc_id = "doc_zip_member"
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id=file_id, filename="Project Kemp/Overview.pptx", path=None)
    _seed_chunk(file_id=file_id, text="Nothing about the member name appears in the extracted text.")
    _seed_source_mapping(file_id=file_id, source_doc_id=doc_id)

    report = repo.ingest_batch(
        nodes=[{"id": "engagement:zipmember", "type": "engagement", "attrs": {}, "evidence": []}],
        edges=[
            {
                "type": "part_of",
                "src": "engagement:zipmember",
                "dst": "project:zipmember",
                "evidence": [{"doc_id": doc_id, "quote": "Project Kemp"}],
            }
        ],
    )
    assert report["claims_written"] == 1
    assert report["claims_rejected"] == []
    assert report["claims_accepted_via_identity"] == 1


def test_verbatim_gate_still_rejects_a_quote_absent_from_chunks_and_identity(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env, file_id="cf_identity2", doc_id="doc_identity2")
    report = repo.ingest_batch(
        nodes=[
            {
                "id": "engagement:fabricated",
                "type": "engagement",
                "attrs": {},
                "evidence": [{"doc_id": doc_id, "quote": "This was never in the document or its name."}],
            }
        ]
    )
    assert report["claims_written"] == 0
    assert len(report["claims_rejected"]) == 1
    assert report["claims_rejected"][0]["reason"] == "verbatim_gate_failed"
    assert report["claims_accepted_via_identity"] == 0


def test_producer_supplied_document_path_does_not_widen_the_identity_haystack(pg_env, repo):
    """Security: `documents[]`' `path` is producer-declared and used only to
    MATCH an existing row — it must never itself become identity evidence,
    or a producer could self-certify an invented quote by declaring
    whatever path it likes on the wire. Only the SERVER-STORED
    `corpus_files.filename`/`path` may widen the gate."""
    file_id = "cf_identity3"
    doc_id = "doc_identity3"
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id=file_id, filename="real-name.pptx", path="Real/Folder/real-name.pptx")
    _seed_chunk(file_id=file_id, text="Nothing relevant here.")
    _seed_source_mapping(file_id=file_id, source_doc_id=doc_id)

    report = repo.ingest_batch(
        documents=[
            {
                "doc_id": doc_id,
                "corpus_id": CORPUS_A,
                "path": "Fake/Spoofed Path — Not Real.pptx",
            }
        ],
        nodes=[
            {
                "id": "engagement:spoof",
                "type": "engagement",
                "attrs": {},
                "evidence": [{"doc_id": doc_id, "quote": "Spoofed Path — Not Real"}],
            }
        ],
    )
    assert report["claims_written"] == 0
    assert report["claims_rejected"][0]["reason"] == "verbatim_gate_failed"
    assert report["claims_accepted_via_identity"] == 0


def test_unicode_normalization_is_consistent_between_identity_and_chunk_paths(pg_env, repo):
    """No normalization is added on either side of the gate — an NFD quote
    against NFC-stored text fails the SAME way whether the text lives in a
    chunk or in the document's own filename/path (a prior live finding was
    an NFC/NFD mismatch; this guards against reintroducing an asymmetry
    between the two haystacks)."""
    import unicodedata as ud

    nfc_word = ud.normalize("NFC", "Café")
    nfd_word = ud.normalize("NFD", "Café")
    assert nfc_word != nfd_word  # sanity: genuinely different code points

    file_id = "cf_identity4"
    doc_id = "doc_identity4"
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(
        file_id=file_id,
        filename=f"{nfc_word} Overview.pptx",
        path=f"Docs/{nfc_word} Overview.pptx",
    )
    _seed_chunk(file_id=file_id, text=f"{nfc_word} is on schedule.")
    _seed_source_mapping(file_id=file_id, source_doc_id=doc_id)

    # Cross-form quote fails against the NFC chunk text (pre-existing, no
    # normalization anywhere in the gate)...
    chunk_mismatch = repo.ingest_batch(nodes=[_node("e:mismatch1", doc_id, f"{nfd_word} is on schedule")])
    assert chunk_mismatch["claims_written"] == 0

    # ...and fails identically against the NFC-stored filename/path.
    identity_mismatch = repo.ingest_batch(nodes=[_node("e:mismatch2", doc_id, f"{nfd_word} Overview.pptx")])
    assert identity_mismatch["claims_written"] == 0
    assert identity_mismatch["claims_rejected"][0]["reason"] == "verbatim_gate_failed"

    # Same-form (NFC) quote succeeds via chunk text (control)...
    chunk_match = repo.ingest_batch(nodes=[_node("e:match1", doc_id, f"{nfc_word} is on schedule")])
    assert chunk_match["claims_written"] == 1

    # ...and via the identity haystack when the chunk text doesn't cover it.
    identity_match = repo.ingest_batch(nodes=[_node("e:match2", doc_id, f"{nfc_word} Overview.pptx")])
    assert identity_match["claims_written"] == 1
    assert identity_match["claims_accepted_via_identity"] == 1


# ---------------------------------------------------------------------------
# Meaningfulness floor — a substring test alone accepts ANY fragment that
# happens to occur literally in the text, including one that carries no
# evidentiary value: a bare file-extension fragment (".pdf") or a lone path
# separator ("/") pass the plain `quote in text` check whenever the document
# happens to mention a filename or a date/fraction/URL anywhere. Live finding:
# both were accepted as claims, `claims_accepted_via_identity == 0`, i.e. via
# the CONTENT half of the gate, not the identity half.
# ---------------------------------------------------------------------------


def test_verbatim_gate_rejects_a_bare_file_extension_quote(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env, text="See the attached report.pdf for details.")
    report = repo.ingest_batch(nodes=[_node("engagement:ext", doc_id, ".pdf")])
    assert report["claims_written"] == 0
    assert len(report["claims_rejected"]) == 1
    # Distinct from `verbatim_gate_failed` on purpose (design decision): the
    # quote WAS found verbatim in the text, so calling this "not found" would
    # mislead an operator. It failed a different check.
    assert report["claims_rejected"][0]["reason"] == "quote_not_meaningful"


def test_verbatim_gate_rejects_a_lone_separator_quote(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env, text="Filed under Project/Reports for review.")
    report = repo.ingest_batch(nodes=[_node("engagement:sep", doc_id, "/")])
    assert report["claims_written"] == 0
    assert report["claims_rejected"][0]["reason"] == "quote_not_meaningful"


def test_verbatim_gate_accepts_a_short_legitimate_acronym_quote(pg_env, repo):
    """The important case: the rule must DISCRIMINATE, not merely be
    strict. "ARR" is a real 3-character metric name (the same shape a bare
    file-extension fragment like "pdf" has once its leading "." is
    stripped) and must still be accepted when it genuinely names something
    in the text."""
    doc_id = _seed_ready_doc(pg_env, text="ARR grew 20% year over year.")
    report = repo.ingest_batch(nodes=[_node("engagement:arr", doc_id, "ARR")])
    assert report["claims_written"] == 1
    assert report["claims_rejected"] == []


def test_meaningfulness_floor_rejects_a_single_character_quote(pg_env, repo):
    """Boundary, low side: a lone word character is technically bounded by
    word characters on both ends (trivially — start and end are the same
    character) but is still too weak to be evidence of anything specific;
    any single letter appears constantly in real text."""
    doc_id = _seed_ready_doc(pg_env, text="Grade A performance across the board.")
    report = repo.ingest_batch(nodes=[_node("engagement:single", doc_id, "A")])
    assert report["claims_written"] == 0
    assert report["claims_rejected"][0]["reason"] == "quote_not_meaningful"


def test_meaningfulness_floor_accepts_a_two_character_quote(pg_env, repo):
    """Boundary, high side: two characters is the floor — a real two-letter
    token (a status code, a country code, a ticker) must still pass."""
    doc_id = _seed_ready_doc(pg_env, text="The deal closed as OK per the review.")
    report = repo.ingest_batch(nodes=[_node("engagement:two", doc_id, "OK")])
    assert report["claims_written"] == 1
    assert report["claims_rejected"] == []


def test_meaningfulness_floor_rejects_a_whitespace_only_quote(pg_env, repo):
    """A run of spaces is truthy (not caught by the pre-existing
    ``empty_quote`` check, which only tests falsiness) and is trivially a
    substring of any multi-word text — closed by the same floor."""
    doc_id = _seed_ready_doc(pg_env, text="Multiple   spaces   appear   here.")
    report = repo.ingest_batch(nodes=[_node("engagement:blank", doc_id, "   ")])
    assert report["claims_written"] == 0
    assert report["claims_rejected"][0]["reason"] == "quote_not_meaningful"


def test_meaningfulness_gate_also_closes_the_identity_path(pg_env, repo):
    """The identity haystack (filename/path) is checked with the exact same
    plain substring test as the chunk text, so a degenerate quote that fails
    the content half would otherwise fall through and be self-certified by
    the document's own filename — nearly every file whose quote is its own
    extension satisfies that trivially. The meaningfulness floor is applied
    ONCE, before either half is tried, so both are covered by one check."""
    file_id = "cf_meaningful1"
    doc_id = "doc_meaningful1"
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id=file_id, filename="Report.pdf", path="Docs/Report.pdf")
    _seed_chunk(file_id=file_id, text="Nothing about the extension appears in the extracted text.")
    _seed_source_mapping(file_id=file_id, source_doc_id=doc_id)

    report = repo.ingest_batch(nodes=[_node("engagement:identity_ext", doc_id, ".pdf")])
    assert report["claims_written"] == 0
    assert report["claims_rejected"][0]["reason"] == "quote_not_meaningful"
    assert report["claims_accepted_via_identity"] == 0


# ---------------------------------------------------------------------------
# C2 — full_documents replace mode drops a stale claim; union mode doesn't.
# ---------------------------------------------------------------------------


def _node(id_, doc_id, quote, attrs=None):
    return {
        "id": id_,
        "type": id_.split(":", 1)[0],
        "attrs": attrs or {},
        "evidence": [{"doc_id": doc_id, "quote": quote}],
    }


def test_c2_full_documents_replace_drops_a_subject_the_reextraction_no_longer_mentions(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env, text="Acme Corp is the client. Beta Corp is a vendor.")
    repo.ingest_batch(
        nodes=[
            _node("engagement:acme", doc_id, "Acme Corp is the client."),
            _node("engagement:beta", doc_id, "Beta Corp is a vendor."),
        ]
    )
    r1 = repo.search(_admin(), type="engagement")
    assert len(r1["subjects"]) == 2

    # Re-extraction drops "beta" entirely; full_documents replace must
    # remove its stale claim so the subject is orphaned and swept.
    report = repo.ingest_batch(
        documents=[],
        full_documents=[doc_id],
        nodes=[_node("engagement:acme", doc_id, "Acme Corp is the client.")],
    )
    assert report["subjects_deleted"] >= 1
    remaining = repo.search(_admin(), type="engagement")
    ids = {s["id"] for s in remaining["subjects"]}
    assert len(ids) == 1


def test_union_mode_default_keeps_a_subject_not_mentioned_in_the_new_batch(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env, text="Acme Corp is the client. Beta Corp is a vendor.")
    repo.ingest_batch(
        nodes=[
            _node("engagement:acme", doc_id, "Acme Corp is the client."),
            _node("engagement:beta", doc_id, "Beta Corp is a vendor."),
        ]
    )
    # Union-mode replay omitting "beta" must NOT delete it.
    repo.ingest_batch(nodes=[_node("engagement:acme", doc_id, "Acme Corp is the client.")])
    remaining = repo.search(_admin(), type="engagement")
    assert len(remaining["subjects"]) == 2


def _admin() -> dict:
    return {"id": "admin1", "email": "admin@test.com"}


# ---------------------------------------------------------------------------
# replay idempotency.
# ---------------------------------------------------------------------------


def test_replaying_the_same_batch_is_a_no_op(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env)
    batch = {"nodes": [_node("engagement:acme-rollout", doc_id, "engagement is underway")]}
    r1 = repo.ingest_batch(**batch)
    assert r1["claims_written"] == 1
    assert r1["subjects_created"] == 1

    r2 = repo.ingest_batch(**batch)
    assert r2["claims_written"] == 0  # ON CONFLICT DO NOTHING — no new claim
    assert r2["subjects_created"] == 0  # alias already resolved

    result = repo.search(_admin(), type="engagement")
    assert len(result["subjects"]) == 1
    assert result["subjects"][0]["claim_count"] == 1  # not duplicated


# ---------------------------------------------------------------------------
# deferred on an unindexed file.
# ---------------------------------------------------------------------------


def test_claim_on_an_unindexed_file_is_deferred_not_rejected(pg_env, repo):
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id="cf_pending", status="processing")
    _seed_chunk(file_id="cf_pending", text="Whatever text eventually lands here.")
    _seed_source_mapping(file_id="cf_pending", source_doc_id="docp")

    report = repo.ingest_batch(nodes=[_node("engagement:x", "docp", "Whatever text")])
    assert report["claims_written"] == 0
    assert report["claims_rejected"] == []
    assert len(report["deferred"]) == 1
    assert report["deferred"][0]["doc_id"] == "docp"
    assert "retry_after_seconds" in report["deferred"][0]


# ---------------------------------------------------------------------------
# unresolved doc_id itemization — documents omitted, doc_id never resolves.
# ---------------------------------------------------------------------------


def test_unresolved_doc_id_with_documents_omitted_rejects_whole_batch(pg_env, repo):
    from src.repositories.facts_pg import IngestUnresolvedDocIds

    with pytest.raises(IngestUnresolvedDocIds) as exc:
        repo.ingest_batch(nodes=[_node("engagement:x", "doc_never_seen", "anything")])
    assert exc.value.unresolved == ["doc_never_seen"]


def test_unresolved_doc_id_with_documents_present_is_itemized_not_whole_batch(pg_env, repo):
    """When `documents[]` IS supplied but one entry still can't resolve
    (content never uploaded), that single claim is itemized in
    claims_rejected rather than failing the whole batch — see the
    ingest_batch docstring in src/repositories/facts_pg.py."""
    doc_id = _seed_ready_doc(pg_env)
    report = repo.ingest_batch(
        documents=[{"doc_id": "phantom", "corpus_id": CORPUS_A, "path": "/nonexistent.md"}],
        nodes=[
            _node("engagement:real", doc_id, "engagement is underway"),
            _node("engagement:ghost", "phantom", "anything"),
        ],
    )
    assert report["claims_written"] == 1
    assert any(r["reason"] == "unresolved_doc_id" for r in report["claims_rejected"])


# ---------------------------------------------------------------------------
# batch caps — never split.
# ---------------------------------------------------------------------------


def test_too_many_documents_raises_batch_too_large(pg_env, repo):
    from src.repositories.facts_pg import MAX_INGEST_DOCUMENTS, IngestBatchTooLarge

    docs = [{"doc_id": f"d{i}", "corpus_id": CORPUS_A, "path": f"/f{i}.md"} for i in range(MAX_INGEST_DOCUMENTS + 1)]
    with pytest.raises(IngestBatchTooLarge) as exc:
        repo.ingest_batch(documents=docs)
    assert exc.value.detail["reason"] == "too_many_documents"


def test_single_document_exceeding_claim_cap_is_a_protocol_error_never_split(pg_env, repo):
    from src.repositories.facts_pg import MAX_INGEST_CLAIMS, IngestDocumentExceedsClaimCap

    evidence = [{"doc_id": "hugedoc", "quote": f"q{i}"} for i in range(MAX_INGEST_CLAIMS + 1)]
    with pytest.raises(IngestDocumentExceedsClaimCap) as exc:
        repo.ingest_batch(nodes=[{"id": "engagement:x", "type": "engagement", "attrs": {}, "evidence": evidence}])
    assert exc.value.doc_id == "hugedoc"
    assert exc.value.count == MAX_INGEST_CLAIMS + 1


def test_total_claims_over_cap_across_many_documents_raises_batch_too_large(pg_env, repo):
    from src.repositories.facts_pg import MAX_INGEST_CLAIMS, IngestBatchTooLarge

    # Spread evidence across many distinct doc_ids so no SINGLE document
    # trips the per-document cap — only the batch total does.
    evidence = [{"doc_id": f"doc{i}", "quote": "q"} for i in range(MAX_INGEST_CLAIMS + 1)]
    with pytest.raises(IngestBatchTooLarge) as exc:
        repo.ingest_batch(nodes=[{"id": "engagement:x", "type": "engagement", "attrs": {}, "evidence": evidence}])
    assert exc.value.detail["reason"] == "too_many_claims"


# ---------------------------------------------------------------------------
# type-conflict rejection.
# ---------------------------------------------------------------------------


def test_type_conflict_on_an_existing_alias_is_rejected(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env)
    fact_id = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=fact_id, type="engagement", natural_key="engagement:acme-rollout")

    report = repo.ingest_batch(
        nodes=[
            {
                "id": "engagement:acme-rollout",
                "type": "acquisition",  # disagrees with the existing alias's stored type
                "attrs": {},
                "evidence": [{"doc_id": doc_id, "quote": "engagement is underway"}],
            }
        ]
    )
    assert report["claims_written"] == 0
    assert report["claims_rejected"][0]["reason"] == "alias_type_conflict"


# ---------------------------------------------------------------------------
# EQ4 — same-date conflict: both claims kept, review item created.
# ---------------------------------------------------------------------------


def test_eq4_same_date_conflict_keeps_both_and_creates_a_review_item(pg_env, repo):
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id="cf_a1")
    _seed_corpus_file(file_id="cf_a2")
    _seed_chunk(file_id="cf_a1", text="The sponsor is Alice Adams as of today.")
    _seed_chunk(file_id="cf_a2", text="The sponsor is Bob Brown as of today.")
    _seed_source_mapping(file_id="cf_a1", source_doc_id="doc1")
    _seed_source_mapping(file_id="cf_a2", source_doc_id="doc2")

    same_day = "2026-03-01T00:00:00Z"
    report = repo.ingest_batch(
        documents=[
            {"doc_id": "doc1", "corpus_id": CORPUS_A, "modified": same_day},
            {"doc_id": "doc2", "corpus_id": CORPUS_A, "modified": same_day},
        ],
        nodes=[
            {
                "id": "engagement:acme-rollout",
                "type": "engagement",
                "attrs": {"sponsor": "Alice Adams"},
                "evidence": [{"doc_id": "doc1", "quote": "The sponsor is Alice Adams"}],
            },
            {
                "id": "engagement:acme-rollout",
                "type": "engagement",
                "attrs": {"sponsor": "Bob Brown"},
                "evidence": [{"doc_id": "doc2", "quote": "The sponsor is Bob Brown"}],
            },
        ],
    )
    assert report["claims_written"] == 2
    assert any(ri["type"] == "attribute_conflict" and ri["key"] == "sponsor" for ri in report["review_items"])

    subject = repo.search(_admin(), type="engagement")["subjects"][0]
    assert subject["attrs"]["sponsor"]["conflicted"] is True


# ---------------------------------------------------------------------------
# EQ5 — different-date succession: later wins, no review item.
# ---------------------------------------------------------------------------


def test_eq5_different_date_succession_later_wins_no_review_item(pg_env, repo):
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id="cf_a1")
    _seed_corpus_file(file_id="cf_a2")
    _seed_chunk(file_id="cf_a1", text="The status is planning.")
    _seed_chunk(file_id="cf_a2", text="The status is active.")
    _seed_source_mapping(file_id="cf_a1", source_doc_id="doc1")
    _seed_source_mapping(file_id="cf_a2", source_doc_id="doc2")

    report = repo.ingest_batch(
        documents=[
            {"doc_id": "doc1", "corpus_id": CORPUS_A, "modified": "2026-01-01T00:00:00Z"},
            {"doc_id": "doc2", "corpus_id": CORPUS_A, "modified": "2026-06-01T00:00:00Z"},
        ],
        nodes=[
            {
                "id": "engagement:acme-rollout",
                "type": "engagement",
                "attrs": {"status": "planning"},
                "evidence": [{"doc_id": "doc1", "quote": "The status is planning."}],
            },
            {
                "id": "engagement:acme-rollout",
                "type": "engagement",
                "attrs": {"status": "active"},
                "evidence": [{"doc_id": "doc2", "quote": "The status is active."}],
            },
        ],
    )
    assert report["claims_written"] == 2
    assert not any(ri["type"] == "attribute_conflict" for ri in report["review_items"])

    subject = repo.search(_admin(), type="engagement")["subjects"][0]
    assert subject["attrs"]["status"] == {"value": "active", "document_date": "2026-06-01"}


# ---------------------------------------------------------------------------
# EQ6 / S8 (write-side) — a `wrong` correction survives re-ingest.
# ---------------------------------------------------------------------------


def test_eq6_wrong_correction_survives_reingest_of_the_same_claims(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env)
    node = _node("engagement:acme-rollout", doc_id, "engagement is underway")
    repo.ingest_batch(nodes=[node])

    fact_id = repo.search(_admin(), type="engagement")["subjects"][0]["id"]
    repo.upsert_correction(
        subject_kind="fact",
        subject_id=fact_id,
        natural_keys=repo.natural_keys_for("fact", fact_id),
        verdict="wrong",
        reason="hallucinated",
        decided_by="admin1",
    )
    assert repo.search(_admin(), type="engagement")["subjects"] == []

    # Re-ingesting the SAME claims must not resurrect visibility.
    repo.ingest_batch(nodes=[node])
    assert repo.search(_admin(), type="engagement")["subjects"] == []


def test_wrong_correction_reattaches_after_the_subject_is_deleted_and_recreated(pg_env, repo):
    """The harder case: the subject is fully DELETED (orphan swept away,
    not merely hidden) and a later ingest re-creates it under a NEW
    surrogate id — the correction must re-attach via natural_keys (spec §3)."""
    doc_id = _seed_ready_doc(pg_env)
    node = _node("engagement:acme-rollout", doc_id, "engagement is underway")
    report1 = repo.ingest_batch(nodes=[node])
    old_fact_id = repo.search(_admin(), type="engagement")["subjects"][0]["id"]
    repo.upsert_correction(
        subject_kind="fact",
        subject_id=old_fact_id,
        natural_keys=repo.natural_keys_for("fact", old_fact_id),
        verdict="wrong",
        reason="hallucinated",
        decided_by="admin1",
    )

    # Full delete: replace mode with an empty node set orphans the subject.
    repo.ingest_batch(documents=[], full_documents=[doc_id], nodes=[])
    assert report1["subjects_created"] == 1

    # Re-create the SAME alias under a fresh surrogate id.
    report2 = repo.ingest_batch(nodes=[node])
    assert report2["subjects_created"] == 1
    new_fact_id = repo.search(_admin(), type="engagement")["subjects"]
    # Still invisible — the reattached `wrong` correction hides it again.
    assert new_fact_id == []
    assert any(c["verdict"] == "wrong" for c in report2["corrections_active"])


# ---------------------------------------------------------------------------
# EQ7 foundation — merge_facts / split_fact reversibility.
# ---------------------------------------------------------------------------


def test_merge_facts_unions_claims_and_aliases_then_split_reverses_it(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env, text="Myers Diligence work started. Myers-Diligence continued.")
    repo.ingest_batch(
        nodes=[
            _node("engagement:myers-diligence", doc_id, "Myers Diligence work started."),
        ]
    )
    duplicate_id = repo.create_fact(type="engagement", natural_key="engagement:myers-dilligence")  # misspelling
    repo.add_claim(
        fact_id=duplicate_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Myers-Diligence continued.",
    )
    canonical_id = repo.search(_admin(), type="engagement")["subjects"][0]["id"]
    if canonical_id == duplicate_id:
        # search() ordering isn't guaranteed; find the OTHER one.
        canonical_id = next(
            s["id"] for s in repo.search(_admin(), type="engagement")["subjects"] if s["id"] != duplicate_id
        )

    snapshot = repo.merge_facts(canonical_id=canonical_id, merged_id=duplicate_id, merged_by="admin1")
    merged = repo.search(_admin(), type="engagement")
    assert len(merged["subjects"]) == 1
    assert merged["subjects"][0]["claim_count"] == 2
    assert set(merged["subjects"][0]["aliases"]) == {"engagement:myers-diligence", "engagement:myers-dilligence"}

    new_id = repo.split_fact(canonical_id=canonical_id, snapshot=snapshot, split_by="admin1")
    after_split = repo.search(_admin(), type="engagement")
    ids = {s["id"] for s in after_split["subjects"]}
    assert ids == {canonical_id, new_id}
    for s in after_split["subjects"]:
        assert s["claim_count"] == 1


def test_merge_facts_survives_shared_evidence_and_split_restores_it(pg_env, repo):
    """`uq_claims_subject_file_quote` is unique on (subject, corpus_file_id,
    quote_hash), so repointing the merged fact's claims explodes with an
    IntegrityError when both facts carry a claim from the SAME document with
    the SAME quote — the ordinary entity-resolution case, where one sentence
    evidences two spellings of the same entity. The merge must survive that,
    and the split must still put the duplicate back (Devin Review on #1652).
    """
    quote = "Acme Corp and Acme Corporation signed."
    doc_id = _seed_ready_doc(pg_env, text=quote)

    canonical_id = repo.create_fact(type="engagement", natural_key="engagement:acme-corp")
    repo.add_alias(fact_id=canonical_id, type="engagement", natural_key="engagement:acme-corp")
    duplicate_id = repo.create_fact(type="engagement", natural_key="engagement:acme-corporation")
    repo.add_alias(fact_id=duplicate_id, type="engagement", natural_key="engagement:acme-corporation")
    del doc_id

    # The SAME document + the SAME quote evidences both facts.
    for fid in (canonical_id, duplicate_id):
        repo.add_claim(fact_id=fid, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote=quote)
    # ...plus one claim only the duplicate holds, which must repoint normally.
    repo.add_claim(
        fact_id=duplicate_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Acme Corporation is the counterparty.",
    )

    snapshot = repo.merge_facts(canonical_id=canonical_id, merged_id=duplicate_id, merged_by="admin1")

    subjects = repo.search(_admin(), type="engagement")["subjects"]
    assert len(subjects) == 1, "the merge must complete, not abort on the shared quote"
    assert subjects[0]["id"] == canonical_id
    # The shared quote is carried by the canonical's own claim, not duplicated.
    assert subjects[0]["claim_count"] == 2
    assert len(snapshot["duplicate_claims"]) == 1
    assert snapshot["duplicate_claims"][0]["quote"] == quote

    new_id = repo.split_fact(canonical_id=canonical_id, snapshot=snapshot, split_by="admin1")
    after = {s["id"]: s for s in repo.search(_admin(), type="engagement")["subjects"]}
    assert set(after) == {canonical_id, new_id}
    # Both sides are back to what they held before the merge: the canonical
    # keeps its shared-quote claim, and the split fact gets its own copy back
    # (fresh id) plus the claim that merely repointed.
    assert after[canonical_id]["claim_count"] == 1
    assert after[new_id]["claim_count"] == 2
    quotes_back = {c["quote"] for c in repo.claims(_admin(), new_id)["claims"]}
    assert quote in quotes_back, "the duplicate claim must be restored, not lost by the merge"


# ---------------------------------------------------------------------------
# C5 — orphan sweep: counted, and a subject with another doc's claim survives.
# ---------------------------------------------------------------------------


def test_c5_orphan_sweep_counts_and_spares_a_subject_with_a_surviving_claim(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env)
    repo.ingest_batch(nodes=[_node("engagement:acme-rollout", doc_id, "engagement is underway")])
    fact_id = repo.search(_admin(), type="engagement")["subjects"][0]["id"]

    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a2")
    _seed_chunk(file_id="cf_a2", text="Acme rollout continues into Q3.")
    repo.add_claim(
        fact_id=fact_id, corpus_file_id="cf_a2", corpus_id=CORPUS_A, file_sha256="sha1", quote="continues into Q3"
    )

    # Delete the FIRST claim's underlying corpus_file (cascades that claim).
    with pg_env.begin() as conn:
        conn.execute(sa.text("DELETE FROM corpus_files WHERE id = 'cf_a1'"))

    deleted = repo.sweep_orphans()
    assert deleted == 0  # the fact still has its cf_a2 claim
    assert len(repo.claims(_admin(), fact_id)["claims"]) == 1


def test_c5_orphan_sweep_deletes_a_subject_with_zero_remaining_claims(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env)
    repo.ingest_batch(nodes=[_node("engagement:acme-rollout", doc_id, "engagement is underway")])

    with pg_env.begin() as conn:
        conn.execute(sa.text("DELETE FROM corpus_files WHERE id = 'cf_a1'"))

    deleted = repo.sweep_orphans()
    assert deleted == 1
    assert repo.search(_admin(), type="engagement")["subjects"] == []


# ---------------------------------------------------------------------------
# C5 refined (spec §0 rev 3.2 / §6) — endpoint evidence: an edge anchors its
# endpoints, so a fact with zero OWN claims survives the sweep as long as an
# incident edge still carries a claim.
# ---------------------------------------------------------------------------


def test_c5_orphan_sweep_spares_a_fact_with_only_a_claimed_incident_edge(pg_env, repo):
    """A fact with ZERO own claims, anchored purely by an incident edge that
    still carries a claim, survives sweep_orphans() — the endpoint-only node
    the producer wire contract (§7.0) explicitly permits."""
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id="cf_a1")

    src = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=src, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Acme.")
    dst = repo.create_fact(type="industry")  # endpoint-only — never given its own claim
    edge_id = repo.create_edge(src=src, type="works_in_industry", dst=dst)
    repo.add_claim(
        edge_id=edge_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Acme is a SaaS company.",
    )

    deleted = repo.sweep_orphans()
    assert deleted == 0
    assert repo.claims(_admin(), dst) == {"claims": [], "revealed": False}


def test_c5_orphan_sweep_deletes_a_fact_when_its_incident_edges_are_also_claimless(pg_env, repo):
    """The refinement is narrower than "any incident edge survives": an edge
    with zero claims of its own is still deleted first (unchanged), and a
    fact anchored ONLY by that now-deleted edge is orphaned too — both
    count."""
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id="cf_a1")

    src = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=src, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Acme.")
    dst = repo.create_fact(type="industry")
    repo.create_edge(src=src, type="works_in_industry", dst=dst)  # no claim ever added

    deleted = repo.sweep_orphans()
    assert deleted == 2  # the claimless edge AND the now-orphaned dst fact

    from src.repositories.facts_pg import FactNotFound

    with pytest.raises(FactNotFound):
        repo.claims(_admin(), dst)


# ---------------------------------------------------------------------------
# Ingest E2E regression — shaped exactly like the live Run P failure: edges
# whose dst is NEVER listed in nodes[] (endpoint-only, spec §7.0) must
# survive the automatic post-ingest sweep and be reachable via neighbors().
# ---------------------------------------------------------------------------


def test_ingest_endpoint_only_edge_destinations_survive_sweep_and_are_reachable(pg_env, repo):
    """Regression for the Run P proving-run finding (spec §0 rev 3.2): a
    batch with a core subject (its own evidence) and several edges whose dst
    is referenced ONLY as an edge endpoint — never in nodes[] — must NOT
    have those dsts (or their edges) swept away by the automatic
    post-ingest orphan sweep, and a depth-1 neighbors() walk from the core
    subject must return every one of its evidenced edges."""
    doc_id = _seed_ready_doc(
        pg_env,
        text=("Acme Corp operates in the SaaS industry. Alice Adams is the CEO. Acme Corp offers Cloud Analytics."),
    )
    report = repo.ingest_batch(
        nodes=[_node("engagement:acme", doc_id, "Acme Corp operates in the SaaS industry.")],
        edges=[
            {
                "src": "engagement:acme",
                "type": "works_in_industry",
                "dst": "industry:saas",
                "evidence": [{"doc_id": doc_id, "quote": "Acme Corp operates in the SaaS industry."}],
            },
            {
                "src": "engagement:acme",
                "type": "has_ceo",
                "dst": "person:alice-adams",
                "evidence": [{"doc_id": doc_id, "quote": "Alice Adams is the CEO."}],
            },
            {
                "src": "engagement:acme",
                "type": "offers_service",
                "dst": "service:cloud-analytics",
                "evidence": [{"doc_id": doc_id, "quote": "Acme Corp offers Cloud Analytics."}],
            },
        ],
    )
    assert report["claims_written"] == 4  # 1 node claim + 3 edge claims
    # None of the endpoint-only dsts, nor the edges naming them, were swept
    # — this is the exact regression the Run P proving run surfaced.
    assert report["subjects_deleted"] == 0

    core_id = repo.search(_admin(), type="engagement")["subjects"][0]["id"]
    result = repo.neighbors(_admin(), core_id, depth=1)
    assert len(result["edges"]) == 3
    assert len(result["nodes"]) == 4  # core + 3 endpoint-only dsts
    for edge in result["edges"]:
        assert edge["src"] == core_id


# ---------------------------------------------------------------------------
# possible_duplicate_of — accepted without evidence, surfaced as review item.
# ---------------------------------------------------------------------------


def test_possible_duplicate_of_edge_needs_no_evidence_and_is_a_review_item(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env, text="Acme Corp is a client. Acme Corporation is a client.")
    report = repo.ingest_batch(
        nodes=[
            _node("engagement:acme", doc_id, "Acme Corp is a client."),
            _node("engagement:acme-corporation", doc_id, "Acme Corporation is a client."),
        ],
        edges=[{"src": "engagement:acme", "type": "possible_duplicate_of", "dst": "engagement:acme-corporation"}],
    )
    assert any(ri["type"] == "possible_duplicate_of" for ri in report["review_items"])


# ---------------------------------------------------------------------------
# §7.3 — functionally single-valued edges (`owned_by`/`for_client` by
# default, `facts.single_valued_edges`): >1 distinct dst with a live claim
# becomes a `single_valued_conflict` review item.
# ---------------------------------------------------------------------------


def test_single_valued_conflict_second_dst_in_same_batch_flags(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env, text="Acme is owned by Alice. Acme is owned by Bob.")
    report = repo.ingest_batch(
        nodes=[
            _node("engagement:acme", doc_id, "Acme is owned by Alice."),
            _node("person:alice", doc_id, "owned by Alice"),
            _node("person:bob", doc_id, "owned by Bob"),
        ],
        edges=[
            {
                "src": "engagement:acme",
                "type": "owned_by",
                "dst": "person:alice",
                "evidence": [{"doc_id": doc_id, "quote": "owned by Alice"}],
            },
            {
                "src": "engagement:acme",
                "type": "owned_by",
                "dst": "person:bob",
                "evidence": [{"doc_id": doc_id, "quote": "owned by Bob"}],
            },
        ],
    )
    conflicts = [ri for ri in report["review_items"] if ri["kind"] == "single_valued_conflict"]
    assert len(conflicts) == 1
    item = conflicts[0]
    assert item["type"] == "owned_by"
    src_fact_id = repo.search(_admin(), type="engagement")["subjects"][0]["id"]
    assert item["src"] == src_fact_id
    assert len(item["dsts"]) == 2


def test_single_valued_conflict_second_dst_in_a_later_batch_flags(pg_env, repo):
    doc1 = _seed_ready_doc(pg_env, file_id="cf_a1", doc_id="doc1", text="Acme is owned by Alice.")
    report1 = repo.ingest_batch(
        nodes=[
            _node("engagement:acme", doc1, "Acme is owned by Alice."),
            _node("person:alice", doc1, "owned by Alice"),
        ],
        edges=[
            {
                "src": "engagement:acme",
                "type": "owned_by",
                "dst": "person:alice",
                "evidence": [{"doc_id": doc1, "quote": "owned by Alice"}],
            }
        ],
    )
    assert not any(ri["kind"] == "single_valued_conflict" for ri in report1["review_items"])

    _seed_corpus_file(file_id="cf_a2")
    _seed_chunk(file_id="cf_a2", text="Acme is owned by Bob.")
    _seed_source_mapping(file_id="cf_a2", source_doc_id="doc2")
    report2 = repo.ingest_batch(
        nodes=[_node("person:bob", "doc2", "owned by Bob")],
        edges=[
            {
                "src": "engagement:acme",
                "type": "owned_by",
                "dst": "person:bob",
                "evidence": [{"doc_id": "doc2", "quote": "owned by Bob"}],
            }
        ],
    )
    conflicts = [ri for ri in report2["review_items"] if ri["kind"] == "single_valued_conflict"]
    assert len(conflicts) == 1
    assert conflicts[0]["type"] == "owned_by"
    assert len(conflicts[0]["dsts"]) == 2


def test_single_valued_conflict_non_listed_edge_type_does_not_flag(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env, text="Acme mentions Alice. Acme mentions Bob.")
    report = repo.ingest_batch(
        nodes=[
            _node("engagement:acme", doc_id, "Acme mentions Alice."),
            _node("person:alice", doc_id, "mentions Alice"),
            _node("person:bob", doc_id, "mentions Bob"),
        ],
        edges=[
            {
                "src": "engagement:acme",
                "type": "mentions",
                "dst": "person:alice",
                "evidence": [{"doc_id": doc_id, "quote": "mentions Alice"}],
            },
            {
                "src": "engagement:acme",
                "type": "mentions",
                "dst": "person:bob",
                "evidence": [{"doc_id": doc_id, "quote": "mentions Bob"}],
            },
        ],
    )
    assert not any(ri["kind"] == "single_valued_conflict" for ri in report["review_items"])


def test_single_valued_edges_config_override_narrows_the_default_set(pg_env, repo, monkeypatch):
    """`facts.single_valued_edges` (instance.yaml) overrides the built-in
    `owned_by`/`for_client` default — an instance that narrows it to an
    empty list stops flagging `owned_by` entirely."""

    def fake_get_value(*keys, default=None):
        return [] if keys == ("facts", "single_valued_edges") else default

    monkeypatch.setattr("app.instance_config.get_value", fake_get_value)

    doc_id = _seed_ready_doc(pg_env, text="Acme is owned by Alice. Acme is owned by Bob.")
    report = repo.ingest_batch(
        nodes=[
            _node("engagement:acme", doc_id, "Acme is owned by Alice."),
            _node("person:alice", doc_id, "owned by Alice"),
            _node("person:bob", doc_id, "owned by Bob"),
        ],
        edges=[
            {
                "src": "engagement:acme",
                "type": "owned_by",
                "dst": "person:alice",
                "evidence": [{"doc_id": doc_id, "quote": "owned by Alice"}],
            },
            {
                "src": "engagement:acme",
                "type": "owned_by",
                "dst": "person:bob",
                "evidence": [{"doc_id": doc_id, "quote": "owned by Bob"}],
            },
        ],
    )
    assert not any(ri["kind"] == "single_valued_conflict" for ri in report["review_items"])


# ---------------------------------------------------------------------------
# TCRD-241 — duplicate doc_id resolution. Byte-identical SharePoint copies
# share a sha-derived doc_id: two (or more) `corpus_file_sources` rows can
# legally share one `source_doc_id`, possibly across different collections.
# Resolution must be corpus-scoped and deterministic, never an arbitrary
# cross-collection pick.
# ---------------------------------------------------------------------------


def _make_group_with_grant(pg_engine, *, group_name: str, collection_id: str, member_user_id: str) -> None:
    from src.repositories import resource_grants_repo, user_group_members_repo, user_groups_repo

    grp = user_groups_repo().create(name=group_name, description="test", created_by="test-fixture")
    user_group_members_repo().add_member(member_user_id, grp["id"], source="admin", added_by="test-fixture")
    resource_grants_repo().create(grp["id"], "collection", collection_id, "test-fixture", "required")


def test_dup_doc_id_two_collections_claim_scoped_to_declaring_corpus_visibility(pg_env, repo):
    """The SAME doc_id anchors a file in both col_a and col_b. The claim is
    declared under col_a (documents[] carries corpus_id=col_a) -- it must
    land on col_a's file and be visible ONLY through col_a's grants, never
    col_b's, even though the resolution had a same-doc_id row to pick from
    in col_b too."""
    from src.repositories import users_repo

    corpus_b = "col_b"
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=corpus_b, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a_dup")
    _seed_corpus_file(corpus_id=corpus_b, file_id="cf_b_dup")
    text = "Acme Corp signed the dup-doc engagement."
    _seed_chunk(corpus_id=CORPUS_A, file_id="cf_a_dup", text=text)
    _seed_chunk(corpus_id=corpus_b, file_id="cf_b_dup", text=text)
    _seed_source_mapping(corpus_id=CORPUS_A, file_id="cf_a_dup", source_doc_id="dupdoc", stable_id="a-path")
    _seed_source_mapping(corpus_id=corpus_b, file_id="cf_b_dup", source_doc_id="dupdoc", stable_id="b-path")

    report = repo.ingest_batch(
        documents=[{"doc_id": "dupdoc", "corpus_id": CORPUS_A, "stable_id": "a-path"}],
        nodes=[_node("engagement:dup", "dupdoc", "Acme Corp signed the dup-doc engagement.")],
    )
    assert report["claims_written"] == 1

    users_repo().create(id="bob", email="bob@test.com", name="Bob")
    _make_group_with_grant(pg_env, group_name="group-b", collection_id=corpus_b, member_user_id="bob")
    bob_view = repo.search({"id": "bob", "email": "bob@test.com"}, type="engagement")
    assert bob_view["subjects"] == []  # col_b grant must not surface the col_a claim

    admin_view = repo.search(_admin(), type="engagement")
    ids = {s["id"] for s in admin_view["subjects"]}
    assert len(ids) == 1  # admin sees it via col_a


def test_dup_doc_id_two_copies_one_corpus_one_batch_is_deterministic_across_replays(pg_env, repo):
    """Both copies of a doc_id anchored in the SAME collection, both declared
    in one `documents[]` batch. Resolution must pick ONE deterministic
    winner -- replaying the identical batch must be a true no-op (a
    non-deterministic pick would sometimes land the replay's claim on the
    OTHER copy, inflating claim_count)."""
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id="cf_1", status="indexed")
    _seed_corpus_file(file_id="cf_2", status="indexed")
    text = "Acme Corp is the client of record."
    _seed_chunk(file_id="cf_1", text=text)
    _seed_chunk(file_id="cf_2", text=text)
    _seed_source_mapping(file_id="cf_1", source_doc_id="dupdoc", stable_id="path1")
    _seed_source_mapping(file_id="cf_2", source_doc_id="dupdoc", stable_id="path2")

    batch = {
        "documents": [
            {"doc_id": "dupdoc", "corpus_id": CORPUS_A, "stable_id": "path1"},
            {"doc_id": "dupdoc", "corpus_id": CORPUS_A, "stable_id": "path2"},
        ],
        "nodes": [_node("engagement:dup2", "dupdoc", "Acme Corp is the client of record.")],
    }
    r1 = repo.ingest_batch(**batch)
    assert r1["claims_written"] == 1

    r2 = repo.ingest_batch(**batch)
    assert r2["claims_written"] == 0  # same winner picked again -> ON CONFLICT no-op

    result = repo.search(_admin(), type="engagement")
    assert len(result["subjects"]) == 1
    assert result["subjects"][0]["claim_count"] == 1  # not doubled across the two copies


def test_dup_doc_id_replace_mode_purges_claims_on_every_anchored_copy(pg_env, repo):
    """`full_documents` replace mode must clear claims off EVERY corpus_file
    anchored to the doc_id within its corpus -- not just the copy this
    ingest's own resolution happens to pick. A stale claim directly attached
    to the non-preferred copy (simulating an earlier extraction that anchored
    there) must not survive a replace."""
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id="cf_x1", status="indexed")
    _seed_corpus_file(file_id="cf_x2", status="indexed")
    text = "Acme Corp is the client."
    _seed_chunk(file_id="cf_x1", text=text)
    _seed_chunk(file_id="cf_x2", text=text)
    _seed_source_mapping(file_id="cf_x1", source_doc_id="dupdoc3", stable_id="x1")
    _seed_source_mapping(file_id="cf_x2", source_doc_id="dupdoc3", stable_id="x2")

    fact_id = repo.create_fact(type="engagement", natural_key="engagement:acme-dup3")
    repo.add_alias(fact_id=fact_id, type="engagement", natural_key="engagement:acme-dup3")
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_x2",  # a stale claim on the OTHER copy
        corpus_id=CORPUS_A,
        file_sha256="",
        quote="Acme Corp is the client.",
    )

    report = repo.ingest_batch(
        documents=[{"doc_id": "dupdoc3", "corpus_id": CORPUS_A, "stable_id": "x1"}],
        full_documents=["dupdoc3"],
        nodes=[_node("engagement:acme-dup3", "dupdoc3", "Acme Corp is the client.")],
    )
    assert report["claims_written"] == 1

    claims = repo.claims(_admin(), fact_id)
    assert len(claims["claims"]) == 1  # the stale one on cf_x2 was purged, not just the resolved copy


def test_dup_doc_id_indexed_copy_preferred_over_pending_not_deferred(pg_env, repo):
    """Two copies anchor the same doc_id in one corpus: one still
    `processing`, one `indexed`. Resolution must prefer the indexed copy so
    the claim attaches instead of being deferred on the OTHER copy's
    not-yet-indexed status."""
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id="cf_pending_dup", status="processing")
    _seed_corpus_file(file_id="cf_indexed_dup", status="indexed")
    text = "The pilot renews annually."
    _seed_chunk(file_id="cf_pending_dup", text=text)
    _seed_chunk(file_id="cf_indexed_dup", text=text)
    _seed_source_mapping(file_id="cf_pending_dup", source_doc_id="dupdoc4", stable_id="p1")
    _seed_source_mapping(file_id="cf_indexed_dup", source_doc_id="dupdoc4", stable_id="p2")

    report = repo.ingest_batch(nodes=[_node("engagement:dup4", "dupdoc4", "The pilot renews annually.")])
    assert report["claims_written"] == 1
    assert report["deferred"] == []


def test_dup_doc_id_document_date_survives_the_indexed_preference_override(pg_env, repo):
    """P2 review finding: `document_date` was looked up by the FILE this
    batch's own `documents[]` entry resolved to, but a SECOND indexed copy
    landing later (TCRD-241) makes the deterministic override pick a
    DIFFERENT winner file -- one this batch never touched, so it has no
    entry in that batch's `doc_dates`. The claim then silently wrote
    `document_date=NULL`, breaking the succession rule (latest date wins):
    an undated claim is treated as inferior to any dated one, so a stale
    value would win forever. `document_date` must travel with the batch's
    OWN declared doc_id, not the resolved file id, so it survives the
    override."""
    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id="cf_1_old", status="indexed", path="docs/old.docx")
    text = "Acme Corp renewal status: in_progress. Later note: Acme Corp renewal status: renewed."
    _seed_chunk(file_id="cf_1_old", text=text)

    # Batch 1: the ONLY copy at this point -- establishes doc_id -> cf_1_old
    # with an early document_date; no TCRD-241 override in play yet.
    r1 = repo.ingest_batch(
        documents=[
            {
                "doc_id": "dupdocdate",
                "corpus_id": CORPUS_A,
                "path": "docs/old.docx",
                "stable_id": "path-old",
                "modified": "2026-01-01",
            }
        ],
        nodes=[
            {
                "id": "engagement:dupdocdate",
                "type": "engagement",
                "attrs": {"status": "in_progress"},
                "evidence": [{"doc_id": "dupdocdate", "quote": "Acme Corp renewal status: in_progress."}],
            }
        ],
    )
    assert r1["claims_written"] == 1

    # A SECOND copy of the SAME doc_id lands, not yet indexed (a fresh
    # SharePoint duplicate just uploaded) -- `cf_1_old` still wins the
    # deterministic (indexed-preferred) tiebreak, even though THIS batch's
    # own documents[] entry resolves to the NEW copy.
    _seed_corpus_file(file_id="cf_2_new", status="processing", path="docs/new.docx")

    r2 = repo.ingest_batch(
        documents=[
            {
                "doc_id": "dupdocdate",
                "corpus_id": CORPUS_A,
                "path": "docs/new.docx",
                "stable_id": "path-new",
                "modified": "2026-03-01",
            }
        ],
        nodes=[
            {
                "id": "engagement:dupdocdate",
                "type": "engagement",
                "attrs": {"status": "renewed"},
                "evidence": [{"doc_id": "dupdocdate", "quote": "Acme Corp renewal status: renewed."}],
            }
        ],
    )
    assert r2["claims_written"] == 1
    assert r2["deferred"] == []  # resolved to the indexed cf_1_old, not the pending cf_2_new

    result = repo.search(_admin(), type="engagement")
    subject = next(s for s in result["subjects"] if "engagement:dupdocdate" in s["aliases"])
    assert subject["attrs"]["status"]["value"] == "renewed"  # succession picked the LATER date

    claims = repo.claims(_admin(), subject["id"])
    dates = sorted(c["document_date"] for c in claims["claims"])
    assert dates == ["2026-01-01", "2026-03-01"]  # neither claim's date silently dropped to null


def test_dup_doc_id_undeclared_doc_id_resolving_only_outside_batch_corpora_is_rejected(pg_env, repo):
    """RBAC review (PR #1736): a claim's doc_id has NO `documents[]` entry
    in THIS batch, and the corpora this batch's `documents[]` DID declare
    don't contain it either -- it must be REJECTED (typed
    `ambiguous_cross_collection_doc_id`), never silently written under
    whatever OTHER, possibly more broadly-granted collection the global
    scan would have found it in. A caller granted only that other
    collection must see nothing -- proof nothing was ever written there."""
    from src.repositories import users_repo

    corpus_b = "col_b"
    doc_a = _seed_ready_doc(pg_env, file_id="cf_a5", doc_id="doc_in_a", text="Acme renewed the contract.")
    _seed_collection(collection_id=corpus_b)
    _seed_corpus_file(corpus_id=corpus_b, file_id="cf_b5")
    _seed_chunk(corpus_id=corpus_b, file_id="cf_b5", text="Unrelated content in another collection.")
    _seed_source_mapping(corpus_id=corpus_b, file_id="cf_b5", source_doc_id="doc_in_b")

    report = repo.ingest_batch(
        documents=[{"doc_id": "doc_in_b", "corpus_id": corpus_b}],
        nodes=[_node("engagement:global5", doc_a, "Acme renewed the contract.")],
    )
    assert report["claims_written"] == 0
    assert report["subjects_created"] == 1  # the node itself still resolves/creates
    assert len(report["claims_rejected"]) == 1
    assert report["claims_rejected"][0]["reason"] == "ambiguous_cross_collection_doc_id"
    assert report["claims_rejected"][0]["doc_id"] == "doc_in_a"

    users_repo().create(id="dana", email="dana@test.com", name="Dana")
    _make_group_with_grant(pg_env, group_name="group-a-broad", collection_id=CORPUS_A, member_user_id="dana")
    dana_view = repo.search({"id": "dana", "email": "dana@test.com"}, type="engagement")
    assert dana_view["subjects"] == []  # nothing was ever written under col_a either


def test_dup_doc_id_documents_present_but_unresolved_entry_does_not_escape_to_global_scan(pg_env, repo):
    """P1 review finding, follow-up to PR #1736: `documents[]` is PRESENT
    (not omitted) but its ONLY entry fails to resolve -- a rename race the
    code tolerates (neither `stable_id` nor `path` matches an existing
    row, and no PRIOR `corpus_file_sources` row exists for this
    (corpus_id, doc_id) either). Because that entry never resolved,
    `batch_corpus_ids` must still be scoped to the corpus THIS batch
    declared -- it must never fall through to tier 3's unrestricted global
    scan and resolve the doc_id under some OTHER, undeclared (possibly
    confidential) collection. Same rejection contract as the
    documents-omitted case: `ambiguous_cross_collection_doc_id`, nothing
    ever written."""
    from src.repositories import users_repo

    corpus_b = "col_b"
    _seed_collection(collection_id=CORPUS_A)
    _seed_collection(collection_id=corpus_b)
    _seed_corpus_file(corpus_id=corpus_b, file_id="cf_b_confidential")
    _seed_chunk(corpus_id=corpus_b, file_id="cf_b_confidential", text="Confidential financials for the deal.")
    _seed_source_mapping(corpus_id=corpus_b, file_id="cf_b_confidential", source_doc_id="renamed_doc")

    report = repo.ingest_batch(
        documents=[
            {
                "doc_id": "renamed_doc",
                "corpus_id": CORPUS_A,
                "stable_id": "stale-stable-id-from-before-the-rename",
            }
        ],
        nodes=[_node("engagement:rename-race", "renamed_doc", "Confidential financials for the deal.")],
    )
    assert report["claims_written"] == 0
    assert len(report["claims_rejected"]) == 1
    assert report["claims_rejected"][0]["reason"] == "ambiguous_cross_collection_doc_id"
    assert report["claims_rejected"][0]["doc_id"] == "renamed_doc"

    users_repo().create(id="carl", email="carl@test.com", name="Carl")
    _make_group_with_grant(pg_env, group_name="group-b-confidential", collection_id=corpus_b, member_user_id="carl")
    carl_view = repo.search({"id": "carl", "email": "carl@test.com"}, type="engagement")
    assert carl_view["subjects"] == []  # nothing was ever written under col_b


def test_dup_doc_id_tier2_hit_resolves_within_another_batch_declared_corpus(pg_env, repo):
    """Multi-corpus batch: an UNDECLARED doc_id (no `documents[]` entry of
    its own) resolves inside one of the OTHER corpora this SAME batch's
    `documents[]` declared. Corpus-safe (never escapes to a collection the
    batch never mentioned at all) -- must resolve normally, not reject."""
    corpus_b = "col_b"
    _seed_ready_doc(pg_env, file_id="cf_t2_x", doc_id="doc_t2_x", text="X content.")
    _seed_collection(collection_id=corpus_b)
    _seed_corpus_file(corpus_id=corpus_b, file_id="cf_t2_y")
    _seed_chunk(corpus_id=corpus_b, file_id="cf_t2_y", text="Y content.")
    _seed_source_mapping(corpus_id=corpus_b, file_id="cf_t2_y", source_doc_id="doc_t2_y")
    _seed_corpus_file(corpus_id=corpus_b, file_id="cf_t2_z")
    _seed_chunk(corpus_id=corpus_b, file_id="cf_t2_z", text="Z content lives in corpus B.")
    _seed_source_mapping(corpus_id=corpus_b, file_id="cf_t2_z", source_doc_id="doc_t2_z")

    report = repo.ingest_batch(
        documents=[
            {"doc_id": "doc_t2_x", "corpus_id": CORPUS_A, "stable_id": "cf_t2_x"},
            {"doc_id": "doc_t2_y", "corpus_id": corpus_b, "stable_id": "cf_t2_y"},
        ],
        nodes=[_node("engagement:t2z", "doc_t2_z", "Z content lives in corpus B.")],
    )
    assert report["claims_written"] == 1
    assert report["claims_rejected"] == []


def test_document_typed_edge_endpoints_are_untouched_by_doc_id_file_resolution(pg_env, repo):
    """`document:<doc_id>` edge endpoints (e.g. a `possible_duplicate_of`
    edge the producer emits between two near-duplicate files) resolve via
    the GENERIC node-alias mechanism (`_resolve_alias` / `fact_aliases`,
    keyed on the literal `natural_key` string) -- a completely different
    code path from the `documents[]`/`corpus_file_sources`-driven doc_id ->
    corpus_file_id resolution this module fixes for EVIDENCE (TCRD-241).

    Proof: this edge resolves successfully even though (a) NEITHER endpoint
    doc_id is declared in `documents[]` this batch, and (b) one of them has
    copies anchored in TWO different corpora -- if endpoint resolution ran
    through the same corpus-scoped/global-fallback ladder as evidence, a
    doc_id absent from `documents[]` would need at least a corpus_file_
    sources row to resolve at all; `_resolve_alias` needs neither. This
    also means an endpoint doc_id that has NO corpus_files row anywhere
    (`document:doc_edge_2` below) still resolves -- endpoint resolution
    never verifies the doc_id names a real file."""
    corpus_b = "col_b"
    _seed_ready_doc(pg_env, file_id="cf_edge_a", doc_id="doc_edge_1", text="Copy one text.")
    _seed_collection(collection_id=corpus_b)
    _seed_corpus_file(corpus_id=corpus_b, file_id="cf_edge_b")
    _seed_chunk(corpus_id=corpus_b, file_id="cf_edge_b", text="Copy one text.")
    _seed_source_mapping(corpus_id=corpus_b, file_id="cf_edge_b", source_doc_id="doc_edge_1")

    report = repo.ingest_batch(
        edges=[{"src": "document:doc_edge_1", "type": "possible_duplicate_of", "dst": "document:doc_edge_2"}]
    )
    assert report["claims_rejected"] == []
    assert report["deferred"] == []
    assert report["subjects_created"] == 2  # both document-entity facts minted fresh
    assert any(ri["type"] == "possible_duplicate_of" for ri in report["review_items"])


# ---------------------------------------------------------------------------
# O7 — source_url: an ingest documents[] entry's citation deep link is
# persisted onto corpus_file_sources so a claim's citation can resolve to
# the source system (spec §8/§8.1).
# ---------------------------------------------------------------------------


def test_documents_source_url_is_persisted_onto_corpus_file_sources(pg_env, repo):
    from src.repositories import corpus_file_sources_repo

    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id="cf_url1")
    _seed_chunk(file_id="cf_url1", text="Acme Corp signed the deal.")
    _seed_source_mapping(file_id="cf_url1", source_doc_id="docurl1", stable_id="p1")

    url = "https://contoso.sharepoint.com/sites/acme/Shared%20Documents/deal.docx"
    report = repo.ingest_batch(
        documents=[{"doc_id": "docurl1", "corpus_id": CORPUS_A, "stable_id": "p1", "source_url": url}],
        nodes=[_node("engagement:url1", "docurl1", "Acme Corp signed the deal.")],
    )
    assert report["claims_written"] == 1
    assert report["source_urls_rejected"] == []  # a valid url is never itemized as rejected

    row = corpus_file_sources_repo().get("cf_url1")
    assert row["source_url"] == url


def test_documents_without_source_url_leaves_it_null(pg_env, repo):
    from src.repositories import corpus_file_sources_repo

    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id="cf_url2")
    _seed_chunk(file_id="cf_url2", text="Acme Corp signed the deal.")
    _seed_source_mapping(file_id="cf_url2", source_doc_id="docurl2", stable_id="p2")

    report = repo.ingest_batch(
        documents=[{"doc_id": "docurl2", "corpus_id": CORPUS_A, "stable_id": "p2"}],
        nodes=[_node("engagement:url2", "docurl2", "Acme Corp signed the deal.")],
    )
    assert report["claims_written"] == 1
    # An ABSENT source_url is the normal case — never itemized as a
    # rejection (only a SENT-but-invalid value is, see the hostile-url test
    # below). A silent NULL here is correct; a silent NULL for a value the
    # producer actually sent is exactly the bug class this counter fixes.
    assert report["source_urls_rejected"] == []

    row = corpus_file_sources_repo().get("cf_url2")
    assert row["source_url"] is None


@pytest.mark.parametrize(
    "hostile_url,expected_reason",
    [
        ("javascript:alert(1)", "not_https"),
        ("data:text/html,<script>alert(1)</script>", "not_https"),
        ("http://contoso.sharepoint.com/deal.docx", "not_https"),  # https-only
        ("https:///deal.docx", "no_host"),  # scheme ok, no host
        ("https://" + "a" * 3000 + ".example.com", "too_long"),  # over the length cap
    ],
)
def test_documents_hostile_source_url_is_dropped_not_stored_claim_still_ingests(
    pg_env, repo, hostile_url, expected_reason
):
    """A hostile/invalid `source_url` must never reach storage (it renders
    as a link's href), but the surrounding claim ingests normally — an
    attacker-controlled producer field must not be able to poison an
    unrelated write (spec §8.1, security playbook). The drop is also no
    longer SILENT (O7 follow-up): it is itemized on the run report's
    `source_urls_rejected`, reasoned, so a non-zero count tells an operator
    their producer is sending urls Agnes won't store."""
    from src.repositories import corpus_file_sources_repo

    _seed_collection(collection_id=CORPUS_A)
    _seed_corpus_file(file_id="cf_url3")
    _seed_chunk(file_id="cf_url3", text="Acme Corp signed the deal.")
    _seed_source_mapping(file_id="cf_url3", source_doc_id="docurl3", stable_id="p3")

    report = repo.ingest_batch(
        documents=[{"doc_id": "docurl3", "corpus_id": CORPUS_A, "stable_id": "p3", "source_url": hostile_url}],
        nodes=[_node("engagement:url3", "docurl3", "Acme Corp signed the deal.")],
    )
    assert report["claims_written"] == 1
    assert report["source_urls_rejected"] == [{"doc_id": "docurl3", "reason": expected_reason}]

    row = corpus_file_sources_repo().get("cf_url3")
    assert row["source_url"] is None


def test_claims_read_shape_carries_source_url_when_present(pg_env, repo):
    doc_id = _seed_ready_doc(pg_env, file_id="cf_url4", doc_id="docurl4")
    url = "https://contoso.sharepoint.com/sites/acme/deal.docx"
    report = repo.ingest_batch(
        documents=[{"doc_id": doc_id, "corpus_id": CORPUS_A, "stable_id": "cf_url4", "source_url": url}],
        nodes=[_node("engagement:url4", doc_id, "The engagement is underway and on schedule.")],
    )
    assert report["claims_written"] == 1

    subject = repo.search(_admin(), type="engagement")["subjects"][0]
    claims = repo.claims(_admin(), subject["id"])["claims"]
    assert claims[0]["document"]["source_url"] == url


# ===========================================================================
# HTTP round-trips — real Postgres backend via build_seeded_client("pg", ...).
# ===========================================================================


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _pg_client(tmp_path, monkeypatch, pg_engine):
    from tests.db_pg._parity_sweep_util import build_seeded_client

    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    return client, admin_token


def test_http_batch_caps_document_count_is_413(tmp_path, monkeypatch, pg_engine):
    from src.repositories.facts_pg import MAX_INGEST_DOCUMENTS

    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    docs = [{"doc_id": f"d{i}", "corpus_id": "col_x", "path": f"/f{i}.md"} for i in range(MAX_INGEST_DOCUMENTS + 1)]
    r = client.post("/api/facts/ingest", json={"documents": docs}, headers=_auth(admin_token))
    assert r.status_code == 413, r.text
    assert r.json()["detail"]["reason"] == "too_many_documents"


def test_http_single_document_exceeding_claim_cap_is_422_never_split(tmp_path, monkeypatch, pg_engine):
    from src.repositories.facts_pg import MAX_INGEST_CLAIMS

    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    evidence = [{"doc_id": "hugedoc", "quote": f"q{i}"} for i in range(MAX_INGEST_CLAIMS + 1)]
    body = {"nodes": [{"id": "engagement:x", "type": "engagement", "attrs": {}, "evidence": evidence}]}
    r = client.post("/api/facts/ingest", json=body, headers=_auth(admin_token))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["reason"] == "document_exceeds_claim_cap"


def test_http_unresolved_doc_id_without_documents_is_400_itemized(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    body = {
        "nodes": [
            {"id": "engagement:x", "type": "engagement", "attrs": {}, "evidence": [{"doc_id": "phantom", "quote": "q"}]}
        ]
    }
    r = client.post("/api/facts/ingest", json=body, headers=_auth(admin_token))
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["doc_ids"] == ["phantom"]


def test_http_corrections_put_then_export_then_delete(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    headers = _auth(admin_token)

    put = client.put(
        "/api/facts/corrections/fact/f_doesnotexist",
        json={"verdict": "wrong", "reason": "hallucinated engagement"},
        headers=headers,
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["verdict"] == "wrong"
    assert body["natural_keys"] == {"aliases": []}

    exported = client.get("/api/facts/corrections", headers=headers)
    assert exported.status_code == 200, exported.text
    rows = exported.json()["corrections"]
    assert any(r["subject_id"] == "f_doesnotexist" for r in rows)

    deleted = client.delete("/api/facts/corrections/fact/f_doesnotexist", headers=headers)
    assert deleted.status_code == 204, deleted.text

    exported_after = client.get("/api/facts/corrections", headers=headers)
    assert not any(r["subject_id"] == "f_doesnotexist" for r in exported_after.json()["corrections"])


# ---------------------------------------------------------------------------
# happy path — real upload through Collections, real chunking, then ingest,
# then a search that finds the resulting subject with a matching quote.
# ---------------------------------------------------------------------------


def test_http_happy_path_upload_then_ingest_then_search_finds_the_subject(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    headers = _auth(admin_token)

    created = client.post("/api/collections", json={"name": "Facts E2E"}, headers=headers)
    assert created.status_code == 201, created.text
    corpus_id = created.json()["id"]

    content = b"Acme Rollout is sponsored by Alice Adams. Work started in March 2026."
    up = client.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("acme.md", io.BytesIO(content), "text/markdown")},
        data={"paths": ["acme.md"]},
        headers=headers,
    )
    assert up.status_code == 201, up.text
    file_id = up.json()[0]["file_id"]

    listing = client.get(f"/api/collections/{corpus_id}/files", headers=headers)
    assert listing.json()["files"][0]["processing_status"] == "indexed", (
        "TestClient runs BackgroundTasks before POST returns"
    )

    ingest_body = {
        "documents": [{"doc_id": "producer-doc-1", "corpus_id": corpus_id, "path": "acme.md"}],
        "nodes": [
            {
                "id": "engagement:acme-rollout",
                "type": "engagement",
                "attrs": {"sponsor": "Alice Adams"},
                "evidence": [{"doc_id": "producer-doc-1", "quote": "Acme Rollout is sponsored by Alice Adams."}],
            }
        ],
    }
    ingest_resp = client.post("/api/facts/ingest", json=ingest_body, headers=headers)
    assert ingest_resp.status_code == 200, ingest_resp.text
    report = ingest_resp.json()
    assert report["claims_written"] == 1
    assert report["claims_rejected"] == []
    assert report["subjects_created"] == 1

    search_resp = client.post("/api/facts/search", json={"type": "engagement"}, headers=headers)
    assert search_resp.status_code == 200, search_resp.text
    subjects = search_resp.json()["subjects"]
    assert len(subjects) == 1
    subject = subjects[0]
    assert subject["claim_count"] == 1
    assert subject["attrs"]["sponsor"]["value"] == "Alice Adams"

    claims_resp = client.get(f"/api/facts/{subject['id']}/claims", headers=headers)
    assert claims_resp.status_code == 200, claims_resp.text
    claims = claims_resp.json()["claims"]
    assert claims[0]["quote"] == "Acme Rollout is sponsored by Alice Adams."
    assert claims[0]["corpus_file_id"] == file_id

    # The ingest above also persisted a run report (spec §7.2/§13.2) — the
    # source card's data. Written OUTSIDE the ingest transaction (see
    # app/api/facts.py::facts_ingest's docstring); this proves the wiring,
    # not just that FactsIngestRunsPgRepository works in isolation (that is
    # tests/db_pg/test_facts_ingest_runs_pg.py's job).
    runs_resp = client.get("/api/facts/ingest-runs", headers=headers)
    assert runs_resp.status_code == 200, runs_resp.text
    runs = runs_resp.json()["runs"]
    assert len(runs) == 1
    run = runs[0]
    assert run["corpus_ids"] == [corpus_id]
    assert run["documents_seen"] == 1
    assert run["claims_written"] == 1
    assert run["claims_rejected_count"] == 0
    assert run["subjects_created"] == 1
    assert run["caller"] == "admin@test.com"
    # No `anonymization` block was sent — the persisted run report still
    # carries the field, defaulted to `{}` (spec §9.2's "never null").
    assert run["anonymization"] == {}


def test_http_anonymization_block_persists_into_the_run_report(tmp_path, monkeypatch, pg_engine):
    """Spec §9.2: an OPTIONAL producer declaration rides into the persisted
    run report (never the direct ingest response) so
    `GET /api/facts/ingest-runs` and the source card can distinguish
    "requested" from "declared"."""
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    headers = _auth(admin_token)

    created = client.post("/api/collections", json={"name": "Anon E2E"}, headers=headers)
    assert created.status_code == 201, created.text
    corpus_id = created.json()["id"]

    content = b"Acme Rollout is sponsored by Alice Adams."
    up = client.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("acme.md", io.BytesIO(content), "text/markdown")},
        data={"paths": ["acme.md"]},
        headers=headers,
    )
    assert up.status_code == 201, up.text

    ingest_body = {
        "documents": [{"doc_id": "producer-doc-anon", "corpus_id": corpus_id, "path": "acme.md"}],
        "nodes": [
            {
                "id": "engagement:acme-rollout-anon",
                "type": "engagement",
                "attrs": {"sponsor": "Alice Adams"},
                "evidence": [{"doc_id": "producer-doc-anon", "quote": "Acme Rollout is sponsored by Alice Adams."}],
            }
        ],
        "anonymization": {
            "declared": True,
            "scopes": {corpus_id: {"docs_anonymized": 1, "docs_skipped": 0}},
        },
    }
    ingest_resp = client.post("/api/facts/ingest", json=ingest_body, headers=headers)
    assert ingest_resp.status_code == 200, ingest_resp.text
    # The direct response is still the plain run report — anonymization is
    # NOT echoed there, only persisted.
    assert "anonymization" not in ingest_resp.json()

    runs_resp = client.get("/api/facts/ingest-runs", headers=headers)
    assert runs_resp.status_code == 200, runs_resp.text
    run = runs_resp.json()["runs"][0]
    assert run["anonymization"] == {
        "declared": True,
        "scopes": {corpus_id: {"docs_anonymized": 1, "docs_skipped": 0}},
    }


# ---------------------------------------------------------------------------
# anonymize-fail-closed gate — end-to-end proof over a real Postgres backend
# that a refused batch writes nothing at all (not merely that the HTTP
# response says 403; tests/test_api_facts_ingest.py already proves the gate
# itself on the DuckDB backend, since it runs before facts_repo() is ever
# reached).
# ---------------------------------------------------------------------------


def _mark_anonymize(client, headers, *, corpus_id: str, name: str) -> None:
    r = client.post(
        "/api/admin/source-connections",
        json={
            "name": name,
            "source_type": "sharepoint",
            "config": {
                "tenant_id": "tenant-1",
                "client_id": "client-1",
                "scopes": [
                    {
                        "source_scope_id": "site1!drive1",
                        "display_path": "Contracts",
                        "anonymize": True,
                        "collection_id": corpus_id,
                    }
                ],
            },
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text


def test_http_anonymize_marked_corpus_without_declaration_writes_nothing(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    headers = _auth(admin_token)

    created = client.post("/api/collections", json={"name": "Anon Gate E2E"}, headers=headers)
    assert created.status_code == 201, created.text
    corpus_id = created.json()["id"]
    _mark_anonymize(client, headers, corpus_id=corpus_id, name="sp-gate-pg")

    content = b"Acme Rollout is sponsored by Alice Adams."
    up = client.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("acme.md", io.BytesIO(content), "text/markdown")},
        data={"paths": ["acme.md"]},
        headers=headers,
    )
    assert up.status_code == 201, up.text

    ingest_body = {
        "documents": [{"doc_id": "producer-doc-gate", "corpus_id": corpus_id, "path": "acme.md"}],
        "nodes": [
            {
                "id": "engagement:acme-rollout-gate",
                "type": "engagement",
                "attrs": {"sponsor": "Alice Adams"},
                "evidence": [{"doc_id": "producer-doc-gate", "quote": "Acme Rollout is sponsored by Alice Adams."}],
            }
        ],
        # No `anonymization` block — the exact failure mode from the report.
    }
    r = client.post("/api/facts/ingest", json=ingest_body, headers=headers)
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "anonymization_not_declared"
    assert detail["corpus_ids"] == [corpus_id]

    # The plaintext content never lands: no subject, no run report either —
    # the refusal happens before FactsPgRepository.ingest_batch is called.
    search_resp = client.post("/api/facts/search", json={"type": "engagement"}, headers=headers)
    assert search_resp.status_code == 200, search_resp.text
    assert search_resp.json()["subjects"] == []

    runs_resp = client.get("/api/facts/ingest-runs", headers=headers)
    assert runs_resp.json()["runs"] == []


def test_http_anonymize_marked_corpus_with_declaration_is_accepted(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    headers = _auth(admin_token)

    created = client.post("/api/collections", json={"name": "Anon Gate Declared E2E"}, headers=headers)
    assert created.status_code == 201, created.text
    corpus_id = created.json()["id"]
    _mark_anonymize(client, headers, corpus_id=corpus_id, name="sp-gate-pg-declared")

    content = b"Acme Rollout is sponsored by Alice Adams."
    up = client.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("acme.md", io.BytesIO(content), "text/markdown")},
        data={"paths": ["acme.md"]},
        headers=headers,
    )
    assert up.status_code == 201, up.text

    ingest_body = {
        "documents": [{"doc_id": "producer-doc-gate-2", "corpus_id": corpus_id, "path": "acme.md"}],
        "nodes": [
            {
                "id": "engagement:acme-rollout-gate-2",
                "type": "engagement",
                "attrs": {"sponsor": "Alice Adams"},
                "evidence": [{"doc_id": "producer-doc-gate-2", "quote": "Acme Rollout is sponsored by Alice Adams."}],
            }
        ],
        "anonymization": {"declared": True, "scopes": {corpus_id: {"docs_anonymized": 1, "docs_skipped": 0}}},
    }
    r = client.post("/api/facts/ingest", json=ingest_body, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["claims_written"] == 1


def test_http_verbatim_gate_rejects_a_fabricated_quote(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    headers = _auth(admin_token)

    created = client.post("/api/collections", json={"name": "Facts Gate"}, headers=headers)
    corpus_id = created.json()["id"]
    up = client.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("doc.md", io.BytesIO(b"The weather was fine on launch day."), "text/markdown")},
        data={"paths": ["doc.md"]},
        headers=headers,
    )
    assert up.status_code == 201, up.text

    ingest_body = {
        "documents": [{"doc_id": "d1", "corpus_id": corpus_id, "path": "doc.md"}],
        "nodes": [
            {
                "id": "engagement:fabricated",
                "type": "engagement",
                "attrs": {},
                "evidence": [{"doc_id": "d1", "quote": "This sentence never appeared anywhere."}],
            }
        ],
    }
    r = client.post("/api/facts/ingest", json=ingest_body, headers=headers)
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["claims_written"] == 0
    assert len(report["claims_rejected"]) == 1
    assert report["claims_rejected"][0]["reason"] == "verbatim_gate_failed"


def test_http_deleting_a_file_via_collections_api_sweeps_orphaned_subjects(tmp_path, monkeypatch, pg_engine):
    """The collections-delete sweep hook (app/api/collections.py::delete_file
    -> _sweep_facts_orphans_after_delete): deleting the file cascades its
    claim, and the subject it solely evidenced is swept."""
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    headers = _auth(admin_token)

    created = client.post("/api/collections", json={"name": "Sweep Hook"}, headers=headers)
    corpus_id = created.json()["id"]
    up = client.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("d.md", io.BytesIO(b"Standalone Corp is a one-off client."), "text/markdown")},
        data={"paths": ["d.md"]},
        headers=headers,
    )
    file_id = up.json()[0]["file_id"]

    ingest_body = {
        "documents": [{"doc_id": "sd1", "corpus_id": corpus_id, "path": "d.md"}],
        "nodes": [
            {
                "id": "engagement:standalone-corp",
                "type": "engagement",
                "attrs": {},
                "evidence": [{"doc_id": "sd1", "quote": "Standalone Corp is a one-off client."}],
            }
        ],
    }
    ingest_resp = client.post("/api/facts/ingest", json=ingest_body, headers=headers)
    assert ingest_resp.json()["claims_written"] == 1

    before = client.post("/api/facts/search", json={"type": "engagement"}, headers=headers)
    assert len(before.json()["subjects"]) == 1

    delete_resp = client.delete(f"/api/collections/{corpus_id}/files/{file_id}", headers=headers)
    assert delete_resp.status_code == 204, delete_resp.text

    after = client.post("/api/facts/search", json={"type": "engagement"}, headers=headers)
    assert after.json()["subjects"] == []
