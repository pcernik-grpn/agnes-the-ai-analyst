"""End-to-end tests for the LLM fact-extraction stage against the REAL
ingest chokepoint (``connectors/sharepoint/facts_extraction.py`` ->
``app.api.facts.facts_ingest``).

PG-only, no DuckDB half to parametrize against (A3 ratchet) — the facts
schema, the ingest run reports and the prompt-override row all live in
Postgres. Seeding mirrors ``tests/db_pg/test_facts_ingest_pg.py``'s style
(raw SQL for ``corpus_files``/``corpus_chunks``, the repo factory for the
mapping row) so the two files agree about what an ingestable document
looks like.

What these tests prove that the unit tests cannot: the batch this stage
produces is accepted BY THE ACTUAL VALIDATORS — the verbatim gate, the
anonymize-fail-closed declaration, the audience format check — rather than
by a mock that agrees with whatever we send.

No network: every test drives a stub extractor.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_A = "col_facts_a"
CONNECTION_ID = "conn-facts-1"


# ---------------------------------------------------------------------------
# fixtures / seeding
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
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from tests.db_pg._parity_sweep_util import _seed_pg_system_groups

    _seed_pg_system_groups(pg_engine)

    from src.repositories import user_group_members_repo, users_repo

    users_repo().create(id="admin1", email="admin@test.com", name="Admin")
    with pg_engine.connect() as conn:
        admin_gid = conn.execute(sa.text("SELECT id FROM user_groups WHERE name = 'Admin'")).scalar()
    user_group_members_repo().add_member("admin1", admin_gid, source="system_seed")

    # The stage attributes its ingest to the scheduler system user; seed it
    # rather than let each test discover the dependency.
    monkeypatch.setattr(
        "connectors.sharepoint.facts_extraction._ingest_identity",
        lambda: {"id": "admin1", "email": "admin@test.com"},
    )
    return pg_engine


def _seed_collection(collection_id: str = CORPUS_A) -> None:
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :slug, :name, :by)"),
            {"id": collection_id, "slug": collection_id, "name": collection_id, "by": "admin1"},
        )


def _seed_document(
    *,
    corpus_id: str = CORPUS_A,
    file_id: str,
    doc_id: str,
    text: str,
    filename: str | None = None,
    path: str | None = None,
    sha256: str | None = None,
    status: str = "indexed",
) -> None:
    """``sha256`` defaults to a value DERIVED from ``file_id`` — distinct
    files get distinct content hashes unless a caller deliberately passes
    the SAME literal ``sha256`` for two files (the LLM-cache tests do
    exactly that, to seed byte-identical documents). A single shared
    literal default here would silently collide two unrelated documents
    under the content-hash cache — a real regression this default exists
    to keep the rest of this file's fixtures from tripping over."""
    if sha256 is None:
        sha256 = "sha-md-" + file_id

    from src.db_pg import get_engine
    from src.repositories import corpus_file_sources_repo

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
        if text:
            conn.execute(
                sa.text(
                    "INSERT INTO corpus_chunks (id, corpus_id, file_id, ordinal, text) "
                    "VALUES (:id, :corpus_id, :file_id, 0, :text)"
                ),
                {
                    "id": "ck_" + secrets.token_hex(8),
                    "corpus_id": corpus_id,
                    "file_id": file_id,
                    "text": text,
                },
            )
    corpus_file_sources_repo().upsert(
        corpus_file_id=file_id,
        corpus_id=corpus_id,
        source_stable_id=f"graph:{file_id}",
        source_doc_id=doc_id,
        source_sha256="sha-src-" + file_id,
    )


def _seed_connection(*, scopes: list[dict] | None = None) -> None:
    from src.repositories import source_connections_repo

    source_connections_repo().create(
        id=CONNECTION_ID,
        name="SharePoint",
        source_type="sharepoint",
        config={
            "scopes": scopes
            if scopes is not None
            else [{"source_scope_id": "site:1", "collection_id": CORPUS_A, "anonymize": False}]
        },
    )


_ONTOLOGY_DOCUMENT = {
    "version": "0.1.0",
    "semantic_model": [
        {
            "name": "corpus ontology",
            "datasets": [
                {
                    "name": "engagement",
                    "source": "ontology_node_type:engagement",
                    "description": "One unit of contracted work.",
                    "fields": [{"name": "name"}],
                },
                {"name": "client", "source": "ontology_node_type:client", "fields": [{"name": "name"}]},
            ],
            "relationships": [{"name": "for_client", "from": "engagement", "to": "client"}],
        }
    ],
}


def _seed_ontology() -> None:
    from src.repositories import semantic_model_repo

    semantic_model_repo().upsert(
        id="sm_ontology",
        slug="corpus-ontology",
        name="corpus ontology",
        description="ontology",
        document=json.dumps(_ONTOLOGY_DOCUMENT),
        document_json=_ONTOLOGY_DOCUMENT,
        spec_version="0.1.0",
        content_hash="hash1",
        source="upload",
        source_ref=None,
        status="valid",
        validation_errors=None,
        validated_at=None,
    )


class StubExtractor:
    """The model seam: canned replies, keyed by the order documents are
    submitted in."""

    def __init__(self, replies, *, model: str = "claude-haiku-4-5") -> None:
        self._replies = list(replies)
        self.model = model
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        self.seen: list[str] = []

    def call(self, user_message: str) -> str:
        self.seen.append(user_message)
        self.usage["calls"] += 1
        self.usage["input_tokens"] += 1000
        self.usage["output_tokens"] += 100
        reply = self._replies[min(len(self.seen) - 1, len(self._replies) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def usage_snapshot(self) -> dict:
        return dict(self.usage)


def _stream(*facts: dict) -> str:
    nodes = [f for f in facts if "id" in f]
    edges = [f for f in facts if "src" in f]
    return "\n".join(["NODES", *[json.dumps(n) for n in nodes], "EDGES", *[json.dumps(e) for e in edges]])


def _run(extractor, **kwargs):
    from connectors.sharepoint.facts_extraction import run_facts_extraction

    return run_facts_extraction(CONNECTION_ID, extractor=extractor, **kwargs)


# ---------------------------------------------------------------------------
# The wire format the real validators accept
# ---------------------------------------------------------------------------


def test_a_pass_writes_claims_through_the_real_ingest_chokepoint(pg_env):
    """The batch this stage builds is accepted BY THE ACTUAL ingest
    validators — not by a mock that agrees with whatever we send."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")

    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {"name": "Northwind rollout"},
        "evidence": [{"doc_id": "doc1", "quote": "rollout began in March"}],
    }
    report = _run(StubExtractor([_stream(node)]))

    assert report["docs_extracted"] == 1
    assert report["nodes_emitted"] == 1
    assert report["claims_written"] == 1
    assert report["claims_rejected"] == 0
    assert report["ingest_failures"] == []

    from src.repositories import facts_repo

    found = facts_repo().search({"id": "admin1"}, type="engagement", filters={}, q=None, limit=10)
    assert [s["id"] for s in found["subjects"]] == ["engagement:northwind-rollout"] or found["subjects"]


def test_an_edge_lands_with_its_endpoints(pg_env):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout is delivered for Contoso Ltd.")

    facts = [
        {
            "id": "engagement:northwind-rollout",
            "type": "engagement",
            "attrs": {},
            "evidence": [{"doc_id": "doc1", "quote": "The Northwind rollout"}],
        },
        {
            "id": "client:contoso-ltd",
            "type": "client",
            "attrs": {},
            "evidence": [{"doc_id": "doc1", "quote": "Contoso Ltd"}],
        },
        {
            "src": "engagement:northwind-rollout",
            "type": "for_client",
            "dst": "client:contoso-ltd",
            "attrs": {},
            "evidence": [{"doc_id": "doc1", "quote": "is delivered for Contoso Ltd"}],
        },
    ]
    report = _run(StubExtractor([_stream(*facts)]))

    assert report["nodes_emitted"] == 2
    assert report["edges_emitted"] == 1
    assert report["claims_written"] == 3
    assert report["claims_rejected"] == 0


def test_a_fabricated_quote_never_reaches_the_server(pg_env):
    """The in-process gate drops it, so `claims_rejected` at the ingest is
    zero — a producer that pre-checks must not show up as somebody else's
    rejection count."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")

    bad = {
        "id": "engagement:x",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "This sentence was never in the document."}],
    }
    report = _run(StubExtractor([_stream(bad), _stream(bad)]))

    assert report["facts_quotes_dropped"] == 1
    assert report["claims_written"] == 0
    assert report["claims_rejected"] == 0


# ---------------------------------------------------------------------------
# Per-connection retry policy override — connection.config.extraction.
# facts.retry_mode wins over the instance-level extraction.facts.retry_mode.
# One high-value connection can keep the corrective retry ON (a dropped
# quote there is a lost citation) while a long-tail connection runs with it
# OFF, without an instance.yaml edit that would flip every connection at
# once.
# ---------------------------------------------------------------------------


def test_a_connections_retry_mode_off_beats_the_instance_default(pg_env):
    """No instance-level `extraction.facts.retry_mode` is set in this test
    env, so the instance default is `on_gate_fail` (retries on a genuine
    failure) — the connection's own `off` override must still win."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")

    from src.repositories import source_connections_repo

    source_connections_repo().config_patch(CONNECTION_ID, {"extraction": {"facts": {"retry_mode": "off"}}})

    bad = {
        "id": "engagement:x",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "This sentence was never in the document."}],
    }
    # Only ONE reply scripted: a retry call reusing the last reply would
    # not error, but the call COUNT below still proves whether it happened.
    extractor = StubExtractor([_stream(bad)])
    report = _run(extractor)

    assert len(extractor.seen) == 1, "the connection's `off` override must suppress the retry entirely"
    assert report["facts_retries"] == 0
    assert report["facts_quotes_dropped"] == 1


def test_a_connection_with_no_override_falls_back_to_the_instance_default(pg_env):
    """The complement of the test above: a connection whose config sets
    nothing under `extraction.facts` behaves exactly like today (the
    instance default, `on_gate_fail` — one retry on a genuine failure)."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")

    bad = {
        "id": "engagement:x",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "This sentence was never in the document."}],
    }
    extractor = StubExtractor([_stream(bad), _stream(bad)])
    report = _run(extractor)

    assert len(extractor.seen) == 2, "no connection override — the instance default (on_gate_fail) retries"
    assert report["facts_retries"] == 1


# ---------------------------------------------------------------------------
# Deterministic quote repair, end to end through the REAL verbatim gate —
# cost-levers spec 2026-09-02 §2.1/§2.2. Proves the repaired quote is not
# just accepted by the in-process pre-check but also by
# `FactsPgRepository.ingest_batch`'s own, independent gate.
# ---------------------------------------------------------------------------


def test_a_normalization_artifact_is_repaired_with_no_retry(pg_env):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The client’s rollout began in March.")

    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        # Straight apostrophe; the stored chunk has a curly one.
        "evidence": [{"doc_id": "doc1", "quote": "client's rollout"}],
    }
    extractor = StubExtractor([_stream(node)])
    report = _run(extractor)

    assert extractor.usage["calls"] == 1, "repaired before any retry — zero extra model calls"
    assert report["facts_retries"] == 0
    assert report["facts_quotes_repaired"] == 1
    assert report["facts_quotes_dropped"] == 0
    assert report["claims_written"] == 1
    assert report["claims_rejected"] == 0


def test_a_quote_spanning_two_chunks_ships_with_no_retry(pg_env):
    """A quote crossing what the extraction happened to split as two
    separate chunks, but present verbatim in the joined text the model was
    shown, passes on the FIRST attempt — no retry, no drop."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="")  # corpus_files row only

    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        for ordinal, chunk_text in enumerate(
            ["The Northwind rollout began in March", "and concluded successfully in April."]
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO corpus_chunks (id, corpus_id, file_id, ordinal, text) "
                    "VALUES (:id, :corpus_id, :file_id, :ordinal, :text)"
                ),
                {
                    "id": "ck_" + secrets.token_hex(8),
                    "corpus_id": CORPUS_A,
                    "file_id": "cf_1",
                    "ordinal": ordinal,
                    "text": chunk_text,
                },
            )

    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "began in March\n\nand concluded successfully"}],
    }
    extractor = StubExtractor([_stream(node)])
    report = _run(extractor)

    assert extractor.usage["calls"] == 1
    assert report["facts_retries"] == 0
    assert report["facts_quotes_dropped"] == 0
    assert report["claims_written"] == 1
    assert report["claims_rejected"] == 0


# ---------------------------------------------------------------------------
# Idempotent re-runs
# ---------------------------------------------------------------------------


def test_a_second_pass_skips_an_unchanged_document(pg_env):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")
    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "rollout began in March"}],
    }

    first = _run(StubExtractor([_stream(node)]))
    assert first["docs_extracted"] == 1

    second_extractor = StubExtractor([_stream(node)])
    second = _run(second_extractor)

    assert second["docs_extracted"] == 0
    assert second["docs_unchanged"] == 1
    assert second_extractor.seen == [], "an unchanged document must cost ZERO model calls"


def test_changed_content_is_re_extracted_and_replaces_its_claims(pg_env):
    """Replace mode (`full_documents`): a re-extraction must not leave the
    claims the new pass no longer makes."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")

    old = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "rollout began in March"}],
    }
    _run(StubExtractor([_stream(old)]))

    # The document's content changed (a re-crawl rewrote the markdown).
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text("UPDATE corpus_files SET sha256 = :s WHERE id = 'cf_1'"),
            {"s": "sha-md-2"},
        )
        conn.execute(sa.text("DELETE FROM corpus_chunks WHERE file_id = 'cf_1'"))
        conn.execute(
            sa.text(
                "INSERT INTO corpus_chunks (id, corpus_id, file_id, ordinal, text) "
                "VALUES (:id, :corpus_id, 'cf_1', 0, :text)"
            ),
            {
                "id": "ck_" + secrets.token_hex(8),
                "corpus_id": CORPUS_A,
                "text": "The Northwind rollout finished in June.",
            },
        )

    new = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "rollout finished in June"}],
    }
    second = _run(StubExtractor([_stream(new)]))
    assert second["docs_extracted"] == 1

    with get_engine().connect() as conn:
        quotes = [r[0] for r in conn.execute(sa.text("SELECT quote FROM claims")).fetchall()]
    assert quotes == ["rollout finished in June"], "the stale claim must be replaced, never accumulated"


def test_an_edited_prompt_forces_a_re_extraction(pg_env, monkeypatch):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")
    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "rollout began in March"}],
    }
    first = _run(StubExtractor([_stream(node)]))
    assert first["docs_extracted"] == 1
    assert first["prompt_origin"] == "builtin"

    from src.repositories import facts_prompt_repo

    facts_prompt_repo().set("EXTRACT DIFFERENTLY", updated_by="admin@test.com")

    second = _run(StubExtractor([_stream(node)]))
    assert second["docs_extracted"] == 1, "a prompt edit must invalidate the per-document state"
    assert second["prompt_origin"] == "admin"


# ---------------------------------------------------------------------------
# Skips and refusals
# ---------------------------------------------------------------------------


def test_a_spreadsheet_is_extracted_like_any_other_document(pg_env):
    """No tabular skip: a spreadsheet's markdown-table conversion goes
    through the exact same walk, model call and verbatim gate as prose —
    its cells and rows can state facts, so it must reach the model."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(
        file_id="cf_1",
        doc_id="doc1",
        text="| Department | Budget |\n| Marketing | 45000 |",
        filename="numbers.md",
        path="Finance/numbers.xlsx",
    )
    node = {
        "id": "engagement:marketing-budget",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "Marketing | 45000"}],
    }

    extractor = StubExtractor([_stream(node)])
    report = _run(extractor)

    assert report["docs_skipped_tabular"] == 0
    assert report["docs_extracted"] == 1
    assert extractor.seen != [], "a spreadsheet's markdown table must reach the model"


def test_a_stale_skipped_tabular_state_entry_is_re_planned(pg_env):
    """A per-document state entry written by an older Agnes version (before
    the tabular skip was removed) must not be trusted as "done" — the next
    pass re-extracts it exactly like a document that was never processed."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(
        file_id="cf_1",
        doc_id="doc1",
        text="| Department | Budget |\n| Marketing | 45000 |",
        filename="numbers.md",
        path="Finance/numbers.xlsx",
    )

    from connectors.sharepoint.facts_extraction import save_state

    save_state(
        CONNECTION_ID,
        {"version": 1, "docs": {"cf_1": {"status": "skipped-tabular", "at": "2026-01-01T00:00:00+00:00"}}},
    )

    node = {
        "id": "engagement:marketing-budget",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "Marketing | 45000"}],
    }
    extractor = StubExtractor([_stream(node)])
    report = _run(extractor)

    assert report["docs_extracted"] == 1
    assert extractor.seen != [], "a stale skipped-tabular entry must not block re-extraction"


def test_a_not_yet_indexed_document_is_left_for_the_next_pass(pg_env):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="Some text.", status="pending")

    extractor = StubExtractor([_stream()])
    report = _run(extractor)

    assert report["docs_skipped_not_indexed"] == 1
    assert extractor.seen == []


def test_an_instance_with_no_ontology_refuses_to_run(pg_env):
    """Extracting against no schema is not a cheaper extraction — it is a
    different, unusable one."""
    from connectors.sharepoint.facts_extraction import FactsExtractionUnavailable

    _seed_collection()
    _seed_connection()
    _seed_document(file_id="cf_1", doc_id="doc1", text="Some text.")

    with pytest.raises(FactsExtractionUnavailable, match="no ontology"):
        _run(StubExtractor([_stream()]))


def test_one_document_failing_never_costs_the_others_their_results(pg_env):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")
    _seed_document(file_id="cf_2", doc_id="doc2", text="The Contoso rollout began in April.")

    good = {
        "id": "engagement:a",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "began in March"}],
    }
    good2 = {
        "id": "engagement:b",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc2", "quote": "began in April"}],
    }

    class Flaky(StubExtractor):
        def call(self, user_message: str) -> str:
            if "doc1" in user_message:
                self.seen.append(user_message)
                raise ValueError("model returned nonsense for this one")
            return super().call(user_message)

    report = _run(Flaky([_stream(good), _stream(good2)]), concurrency=1)

    assert report["facts_failed"] == 1
    assert report["docs_extracted"] == 1, "the healthy document still landed"
    assert report["claims_written"] == 1


def test_a_permanent_model_error_fails_only_that_document(pg_env):
    """A 400 ``invalid_request_error`` (e.g. "prompt is too long") on the
    SYNC transport fails only THAT document: `facts_failed` counts it (with
    `facts_failed_reasons` naming why), the healthy document still lands,
    and the pass returns NORMALLY rather than raising
    `FactsExtractionUnavailable` — the sync-transport half of the same
    contract the batch transport's `invalid_request` handling
    (`_requeue_or_fail`) already has. Mirrors
    `test_one_document_failing_never_costs_the_others_their_results`, but
    for the SPECIFIC permanent-error class the live 2026-09 incident hit."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")
    _seed_document(file_id="cf_2", doc_id="doc2", text="The Contoso rollout began in April.")

    good2 = {
        "id": "engagement:b",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc2", "quote": "began in April"}],
    }

    from connectors.sharepoint.facts_extraction import FactsDocumentError

    class OneDocPermanentlyFails(StubExtractor):
        def call(self, user_message: str) -> str:
            if "doc1" in user_message:
                self.seen.append(user_message)
                raise FactsDocumentError(
                    "fact extraction permanently failed (invalid_request): BadRequestError: "
                    "prompt is too long: 316295 tokens > 200000 maximum",
                    reason="invalid_request",
                )
            return super().call(user_message)

    report = _run(OneDocPermanentlyFails([_stream(good2)]), concurrency=1)

    assert report["facts_failed"] == 1
    assert report["facts_failed_reasons"] == {"invalid_request": 1}
    assert report["docs_extracted"] == 1, "the healthy document still landed"
    assert report["claims_written"] == 1


def test_an_unreachable_model_stops_the_pass_loudly(pg_env):
    """Never a silent "0 facts": that is indistinguishable from a corpus
    that genuinely has none."""
    from connectors.sharepoint.facts_extraction import FactsExtractionUnavailable

    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="Some text.")

    with pytest.raises(FactsExtractionUnavailable):
        _run(StubExtractor([FactsExtractionUnavailable("no credential")]))


def test_a_provider_limit_hit_ends_the_sync_pass_cleanly_instead_of_failing_the_job(pg_env):
    """A closed-set provider refusal (TCRD-296 synthesis F.25) is NOT the
    same failure posture as an unreachable model above: the pass completes
    with ``interrupted_reason: "provider_limit"`` rather than raising —
    the whole point is that this ends up a `done` job, not a `failed` one,
    so the crawl's streamed trigger has a condition to check instead of a
    failed-job row to retry blindly."""
    from connectors.sharepoint.facts_extraction import ProviderLimitHit
    from src.repositories import extraction_conditions_repo

    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="Some text.")

    report = _run(
        StubExtractor([ProviderLimitHit("workspace usage limit hit", reason="workspace_limit", retry_after_s=None)])
    )
    assert report["interrupted"] is True
    assert report["interrupted_reason"] == "provider_limit"

    conditions = extraction_conditions_repo().list_active()
    assert len(conditions) == 1
    assert conditions[0]["reason"] == "workspace_limit"
    assert conditions[0]["provider"] == "anthropic"


def test_a_successful_sync_pass_clears_an_active_provider_limit_condition(pg_env):
    """The signal the provider is answering again: once a pass for a
    provider completes WITHOUT hitting a refusal, whatever condition that
    provider had active is cleared — this is what makes the manual trigger
    (``POST …/facts-extract``) a real "clear" action rather than a no-op."""
    from src.repositories import extraction_conditions_repo

    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")

    extraction_conditions_repo().record(
        reason="workspace_limit",
        provider="anthropic",
        model="claude-haiku-4-5",
        region=None,
        message="stale condition from an earlier pass",
        retry_after_s=None,
    )
    assert extraction_conditions_repo().list_active() != []

    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {"name": "Northwind rollout"},
        "evidence": [{"doc_id": "doc1", "quote": "rollout began in March"}],
    }
    report = _run(StubExtractor([_stream(node)]))
    assert report["interrupted"] is False

    assert extraction_conditions_repo().list_active() == []


# ---------------------------------------------------------------------------
# Anonymization declaration — the fail-closed ingest gate
# ---------------------------------------------------------------------------


def test_an_anonymize_marked_collection_is_declared_so_the_gate_accepts_it(pg_env):
    """The ingest REFUSES a batch touching an anonymize-marked corpus that
    the batch does not declare (403 anonymization_not_declared). This pass
    must declare it — the documents it quotes were anonymized by the crawl
    before they were ever ingested."""
    _seed_collection()
    _seed_connection(scopes=[{"source_scope_id": "site:1", "collection_id": CORPUS_A, "anonymize": True}])
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="PERSON_a1b2c3 led the Northwind rollout.")

    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "led the Northwind rollout"}],
    }
    report = _run(StubExtractor([_stream(node)]))

    assert report["ingest_failures"] == [], "an undeclared anonymize-marked corpus would 403 here"
    assert report["claims_written"] == 1

    from src.repositories import facts_ingest_runs_repo

    runs = facts_ingest_runs_repo().list_recent(limit=5)
    assert runs and runs[0]["anonymization"]["declared"] is True
    assert runs[0]["anonymization"]["scopes"][CORPUS_A]["docs_anonymized"] == 1


def test_a_refused_batchs_ledger_downgrade_survives_a_pass_whose_every_flush_is_refused(pg_env, monkeypatch):
    """TCRD-296 gap #62: the ledger correction a refused flush applies
    (``_BatchShipper._revert_ledger``) must be PERSISTED, not merely held
    in memory — a pass with exactly one batch, and that batch refused,
    never reaches the success branch's own ``save_state`` call. Forces a
    REAL ingest refusal (the anonymize-fail-closed gate, undeclared) rather
    than a mock, so this proves the real `POST …/facts/ingest` -> shipper
    -> ledger path end to end."""
    _seed_collection()
    _seed_connection(scopes=[{"source_scope_id": "site:1", "collection_id": CORPUS_A, "anonymize": True}])
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="PERSON_a1b2c3 led the Northwind rollout.")

    # The producer-side declaration this pass would normally make on its
    # own — suppressed, so the SAME undeclared-anonymize-marked-corpus gate
    # `test_an_anonymize_marked_collection_is_declared_so_the_gate_accepts_it`
    # proves accepts a correct declaration now refuses this one instead.
    monkeypatch.setattr("connectors.sharepoint.facts_extraction.anonymize_marked_collection_ids", lambda conn: set())

    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "led the Northwind rollout"}],
    }
    report = _run(StubExtractor([_stream(node)]))

    assert report["docs_extracted"] == 1
    assert len(report["ingest_failures"]) == 1
    assert report["ingest_failures"][0]["status"] == 403
    assert report["claims_written"] == 0

    from connectors.sharepoint.facts_extraction import load_state

    # A FRESH load — proves the correction was actually written to the
    # state store, not merely mutated on the in-memory `docs_state` this
    # run's own `state` object happened to hold.
    persisted = load_state(CONNECTION_ID)
    entry = persisted["docs"]["cf_1"]
    assert entry["status"] == "ingest_refused"
    assert entry["retry_count"] == 1


# ---------------------------------------------------------------------------
# Usage accounting + concurrency
# ---------------------------------------------------------------------------


def test_the_run_reports_what_it_spent(pg_env):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")
    node = {
        "id": "engagement:a",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "began in March"}],
    }

    report = _run(StubExtractor([_stream(node)]))
    usage = report["facts_usage"]

    assert usage["calls"] == 1
    assert usage["input_tokens"] == 1000
    assert usage["documents"] == 1
    assert usage["model"] == "claude-haiku-4-5"
    assert usage["estimated_cost_usd"] > 0

    from src.repositories import facts_ingest_runs_repo

    runs = facts_ingest_runs_repo().list_recent(limit=5)
    assert runs[0]["llm_usage"]["input_tokens"] == 1000


# ---------------------------------------------------------------------------
# Content-hash LLM response cache (cost-levers spec 2026-09-02, lever B) —
# end to end against the REAL `facts_llm_cache` table.
# ---------------------------------------------------------------------------


def test_facts_llm_cache_pg_repository_round_trips(pg_env):
    from src.repositories import facts_llm_cache_repo

    repo = facts_llm_cache_repo()
    assert repo.get("missing-key") is None

    repo.put(
        "key1",
        sha256="sha-1",
        model="claude-haiku-4-5",
        fingerprint="fp1",
        response={"text": "NODES\nEDGES\n"},
        usage={"input_tokens": 1000, "output_tokens": 100},
    )
    row = repo.get("key1")
    assert row["response"] == {"text": "NODES\nEDGES\n"}
    assert row["usage"] == {"input_tokens": 1000, "output_tokens": 100}
    assert row["sha256"] == "sha-1"
    assert row["model"] == "claude-haiku-4-5"
    assert row["fingerprint"] == "fp1"

    stats = repo.stats()
    assert stats == {"rows": 1, "distinct_documents": 1}

    # Upsert: the SAME key overwrites its own row rather than erroring.
    repo.put(
        "key1", sha256="sha-1", model="claude-haiku-4-5", fingerprint="fp1", response={"text": "NODES\nEDGES\nmore"}
    )
    assert repo.get("key1")["response"] == {"text": "NODES\nEDGES\nmore"}
    assert repo.stats()["rows"] == 1

    assert repo.clear() == 1
    assert repo.get("key1") is None
    assert repo.stats() == {"rows": 0, "distinct_documents": 0}


def test_a_byte_identical_document_is_served_from_cache_not_a_second_model_call(pg_env):
    """The lever's own scenario: a consultancy keeps several copies of the
    same document (v1/v2/final in different folders) — two different
    files, same content hash, cost ONE model call between them."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    text = "The Northwind rollout began in March."
    _seed_document(file_id="cf_1", doc_id="doc1", text=text, sha256="sha-dup-1")
    _seed_document(file_id="cf_2", doc_id="doc2", text=text, sha256="sha-dup-1")

    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "rollout began in March"}],
    }
    # Only ONE reply scripted — a second real call would reuse it (the stub
    # clamps its index), so the call COUNT below is what actually proves
    # the cache, not merely "the pass didn't crash".
    extractor = StubExtractor([_stream(node)])
    report = _run(extractor)

    assert report["docs_extracted"] == 2
    assert len(extractor.seen) == 1, "the second, byte-identical document must cost zero model calls"
    assert report["facts_usage"]["cache_hits"] == 1

    from src.repositories import facts_llm_cache_repo

    assert facts_llm_cache_repo().stats()["rows"] == 1


def test_a_cache_served_reply_ships_claims_under_the_serving_documents_own_doc_id(pg_env):
    """TCRD-296 gap #62: cf_1 and cf_2 share a converted-markdown hash (the
    LLM-cache key) but are DIFFERENT documents (different doc_id) — a
    legitimate cache hit whose replayed reply, uncorrected, still cites
    cf_1's doc_id. The claim the cache-served reply produces for cf_2 must
    land on cf_2, never silently attach to cf_1 or get rejected."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    text = "The Northwind rollout began in March."
    _seed_document(file_id="cf_1", doc_id="doc1", text=text, sha256="sha-dup-2")
    _seed_document(file_id="cf_2", doc_id="doc2", text=text, sha256="sha-dup-2")

    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "rollout began in March"}],
    }
    extractor = StubExtractor([_stream(node)])
    report = _run(extractor)

    assert report["docs_extracted"] == 2
    assert len(extractor.seen) == 1, "the cache hit must still cost zero model calls"
    assert report["claims_written"] == 2, "each document's own evidence is a separate, valid claim"
    assert report["claims_rejected"] == 0
    assert report["facts_evidence_doc_id_rewritten"] == 1

    from src.db_pg import get_engine

    with get_engine().connect() as conn:
        rows = conn.execute(sa.text("SELECT corpus_file_id FROM claims ORDER BY corpus_file_id")).mappings().all()
    assert sorted(r["corpus_file_id"] for r in rows) == ["cf_1", "cf_2"]


def test_llm_cache_can_be_disabled_even_on_postgres(pg_env, monkeypatch):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    text = "The Northwind rollout began in March."
    _seed_document(file_id="cf_1", doc_id="doc1", text=text, sha256="sha-dup-2")
    _seed_document(file_id="cf_2", doc_id="doc2", text=text, sha256="sha-dup-2")

    monkeypatch.setenv("AGNES_EXTRACTION_FACTS_LLM_CACHE", "0")

    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "rollout began in March"}],
    }
    extractor = StubExtractor([_stream(node), _stream(node)])
    report = _run(extractor)

    assert report["docs_extracted"] == 2
    assert len(extractor.seen) == 2, "the cache is off — both documents call the model"
    assert report["facts_usage"]["cache_hits"] == 0


@pytest.mark.parametrize("workers", [1, 4])
def test_concurrency_does_not_change_the_outcome(pg_env, workers):
    """The knob buys wall clock, never a different result: same documents,
    same claims, same counters at 1 and at 4."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    for i in range(6):
        _seed_document(
            file_id=f"cf_{i}",
            doc_id=f"doc{i}",
            text=f"The rollout number {i} began in March.",
        )

    def reply(message: str) -> str:
        doc_id = json.loads(message.split("```json\n", 1)[1].split("\n```", 1)[0])["doc_id"]
        index = doc_id.removeprefix("doc")
        return _stream(
            {
                "id": f"engagement:rollout-{index}",
                "type": "engagement",
                "attrs": {},
                "evidence": [{"doc_id": doc_id, "quote": f"rollout number {index} began in March"}],
            }
        )

    class Scripted(StubExtractor):
        def call(self, user_message: str) -> str:
            self.seen.append(user_message)
            self.usage["calls"] += 1
            self.usage["input_tokens"] += 1000
            return reply(user_message)

    report = _run(Scripted([]), concurrency=workers)

    assert report["docs_extracted"] == 6
    assert report["claims_written"] == 6
    assert report["facts_quotes_dropped"] == 0
    assert report["concurrency"] == workers

    from src.db_pg import get_engine

    with get_engine().connect() as conn:
        quotes = sorted(r[0] for r in conn.execute(sa.text("SELECT quote FROM claims")).fetchall())
    assert quotes == sorted(f"rollout number {i} began in March" for i in range(6))


def test_concurrency_source_is_reported(pg_env, monkeypatch):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="Some text here.")

    report = _run(StubExtractor([_stream()]))
    assert report["concurrency"] == 3
    assert report["concurrency_source"] == "default"


def test_a_multi_batch_pass_reports_each_batchs_own_spend(pg_env, monkeypatch):
    """`GET /api/facts/ingest-runs` SUMS every persisted run's `llm_usage`,
    so a batch reporting the run's running total would inflate a
    multi-batch pass's cost — a 3-batch pass would read as 2x what it
    actually spent."""
    import connectors.sharepoint.facts_extraction as stage

    monkeypatch.setattr(stage, "DEFAULT_BATCH_DOCUMENTS", 1)  # one batch per document

    _seed_collection()
    _seed_connection()
    _seed_ontology()
    for i in range(3):
        _seed_document(file_id=f"cf_{i}", doc_id=f"doc{i}", text=f"The rollout number {i} began in March.")

    def reply(message: str) -> str:
        doc_id = json.loads(message.split("```json\n", 1)[1].split("\n```", 1)[0])["doc_id"]
        index = doc_id.removeprefix("doc")
        return _stream(
            {
                "id": f"engagement:rollout-{index}",
                "type": "engagement",
                "attrs": {},
                "evidence": [{"doc_id": doc_id, "quote": f"rollout number {index} began in March"}],
            }
        )

    class Scripted(StubExtractor):
        def call(self, user_message: str) -> str:
            self.seen.append(user_message)
            self.usage["calls"] += 1
            self.usage["input_tokens"] += 1000
            return reply(user_message)

    report = _run(Scripted([]), concurrency=1)
    assert report["ingest_batches"] == 3
    assert report["facts_usage"]["input_tokens"] == 3000

    from src.repositories import facts_ingest_runs_repo

    runs = facts_ingest_runs_repo().list_recent(limit=10)
    assert len(runs) == 3
    per_batch = [r["llm_usage"]["input_tokens"] for r in runs]
    assert sorted(per_batch) == [1000, 1000, 1000], "each batch reports ITS OWN spend, not the running total"
    assert sum(per_batch) == report["facts_usage"]["input_tokens"]


def test_a_multi_batch_pass_sweeps_orphans_exactly_once(pg_env, monkeypatch):
    """TCRD-296 C.12: a 3-batch pass calls ``sweep_orphans()`` exactly
    ONCE, after the whole pass has shipped — not once per batch (the live
    finding: 7 parallel passes each sweeping per batch deleted 75,447
    subjects against 10,784 created in 30 minutes, ~13% of documents
    failing on a foreign-key violation). A pre-existing, genuinely stale
    orphan proves the single sweep that DOES run is a real one, not a
    no-op — and the pass report carries the count."""
    import connectors.sharepoint.facts_extraction as stage
    from src.db_pg import get_engine
    from src.repositories import facts_repo
    from src.repositories.facts_pg import FactsPgRepository

    monkeypatch.setattr(stage, "DEFAULT_BATCH_DOCUMENTS", 1)  # one batch per document

    _seed_collection()
    _seed_connection()
    _seed_ontology()
    for i in range(3):
        _seed_document(file_id=f"cf_{i}", doc_id=f"doc{i}", text=f"The rollout number {i} began in March.")

    stale_id = facts_repo().create_fact(type="engagement", natural_key="engagement:pre-existing-orphan")
    with get_engine().begin() as conn:
        conn.execute(
            sa.text("UPDATE facts SET created_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": stale_id},
        )

    calls = {"n": 0}
    real_sweep = FactsPgRepository.sweep_orphans

    def _counting_sweep(self, **kwargs):
        calls["n"] += 1
        return real_sweep(self, **kwargs)

    monkeypatch.setattr(FactsPgRepository, "sweep_orphans", _counting_sweep)

    def reply(message: str) -> str:
        doc_id = json.loads(message.split("```json\n", 1)[1].split("\n```", 1)[0])["doc_id"]
        index = doc_id.removeprefix("doc")
        return _stream(
            {
                "id": f"engagement:rollout-{index}",
                "type": "engagement",
                "attrs": {},
                "evidence": [{"doc_id": doc_id, "quote": f"rollout number {index} began in March"}],
            }
        )

    class Scripted(StubExtractor):
        def call(self, user_message: str) -> str:
            self.seen.append(user_message)
            self.usage["calls"] += 1
            self.usage["input_tokens"] += 1000
            return reply(user_message)

    report = _run(Scripted([]), concurrency=1)

    assert report["ingest_batches"] == 3
    assert calls["n"] == 1, "sweep_orphans() must run once per PASS, not once per batch"
    assert report["orphans_swept"] >= 1
    assert report["orphans_sweep_skipped"] is False

    with get_engine().connect() as conn:
        still_there = conn.execute(sa.text("SELECT 1 FROM facts WHERE id = :id"), {"id": stale_id}).scalar()
    assert still_there is None, "the pass's own single end-of-pass sweep must still reap a real orphan"


def test_an_expired_deadline_stops_between_documents_and_keeps_what_it_paid_for(pg_env):
    """The crawl that preceded this pass genuinely finished, so the run is
    not failed — the block says `interrupted: timeout`, whatever was
    already extracted is SHIPPED (those calls are paid for either way), and
    the per-document state means the rest is simply next run's work."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    for i in range(4):
        _seed_document(file_id=f"cf_{i}", doc_id=f"doc{i}", text=f"The rollout number {i} began in March.")

    class ExpiringDeadline:
        """Expires once one document has been submitted."""

        def __init__(self) -> None:
            self.checks = 0

        @property
        def expired(self) -> bool:
            self.checks += 1
            return self.checks > 1

    def reply(message: str) -> str:
        doc_id = json.loads(message.split("```json\n", 1)[1].split("\n```", 1)[0])["doc_id"]
        index = doc_id.removeprefix("doc")
        return _stream(
            {
                "id": f"engagement:rollout-{index}",
                "type": "engagement",
                "attrs": {},
                "evidence": [{"doc_id": doc_id, "quote": f"rollout number {index} began in March"}],
            }
        )

    class Scripted(StubExtractor):
        def call(self, user_message: str) -> str:
            self.seen.append(user_message)
            self.usage["calls"] += 1
            return reply(user_message)

    report = _run(Scripted([]), concurrency=1, deadline=ExpiringDeadline())

    assert report["interrupted"] is True
    assert report["interrupted_reason"] == "timeout"
    assert report["docs_extracted"] == 1, "the one document already submitted was drained, not abandoned"
    assert report["claims_written"] == 1

    # …and the next run picks up where this one stopped.
    second = _run(Scripted([]))
    assert second["docs_extracted"] == 3
    assert second["docs_unchanged"] == 1


# ---------------------------------------------------------------------------
# `on_progress` — the liveness seam (owner-frustration fix, 2026-09-02): a
# healthy multi-hour facts pass never checkpointed at all, so the crawl's own
# `_STALL_AFTER_S`-derived liveness check declared it dead the longer (and
# more expensive) it ran. This proves the REAL walk/submit/drain loop calls
# it, not just the wiring closure on the crawler side (covered separately in
# `tests/test_sharepoint_crawler.py::TestFactsProgressCheckpointing`).
# ---------------------------------------------------------------------------


def test_on_progress_fires_before_the_first_document_and_after_each_one(pg_env):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")
    _seed_document(file_id="cf_2", doc_id="doc2", text="The Contoso rollout began in April.")

    node1 = {
        "id": "engagement:a",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "began in March"}],
    }
    node2 = {
        "id": "engagement:b",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc2", "quote": "began in April"}],
    }

    updates: list[dict] = []
    _run(
        StubExtractor([_stream(node1), _stream(node2)]),
        concurrency=1,  # deterministic submission/drain order
        on_progress=lambda update: updates.append(dict(update)),
    )

    # Fired once BEFORE the first submission — the honest "we don't know
    # yet" value — so a caller wired to a run recorder can flip its row's
    # phase to "facts" immediately rather than only once a (possibly slow)
    # first document finishes.
    assert updates[0] == {"docs_done": 0, "docs_total": 0, "current_path": None}
    # …then once per document, `docs_total` GROWING as the walk discovers
    # more candidates — never invented ahead of what has actually been
    # submitted (the same honesty rule `files_seen` follows on the crawl
    # side).
    assert updates[1] == {"docs_done": 1, "docs_total": 1, "current_path": "cf_1.md"}
    assert updates[2] == {"docs_done": 2, "docs_total": 2, "current_path": "cf_2.md"}
    assert len(updates) == 3


def test_on_progress_counts_a_failed_document_as_done_too(pg_env):
    """A document that errors is still one fewer left — the checkpoint this
    drives must move even on a run that is about to fail one document."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")
    _seed_document(file_id="cf_2", doc_id="doc2", text="The Contoso rollout began in April.")

    good = {
        "id": "engagement:b",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc2", "quote": "began in April"}],
    }

    class Flaky(StubExtractor):
        def call(self, user_message: str) -> str:
            if "doc1" in user_message:
                self.seen.append(user_message)
                raise ValueError("model returned nonsense")
            return super().call(user_message)

    updates: list[dict] = []
    report = _run(
        Flaky([_stream(good)]),
        concurrency=1,
        on_progress=lambda update: updates.append(dict(update)),
    )

    assert report["facts_failed"] == 1
    docs_done_sequence = [u["docs_done"] for u in updates]
    assert docs_done_sequence == [0, 1, 2], "the failed document still advances docs_done"


def test_on_progress_is_never_called_when_the_stage_refuses_to_run(pg_env):
    """No corpus was walked, so there is nothing to report progress on —
    never a call with invented zeros for a pass that never started."""
    from connectors.sharepoint.facts_extraction import FactsExtractionUnavailable

    _seed_collection()
    _seed_connection()
    _seed_document(file_id="cf_1", doc_id="doc1", text="Some text.")

    updates: list[dict] = []
    with pytest.raises(FactsExtractionUnavailable, match="no ontology"):
        _run(StubExtractor([_stream()]), on_progress=lambda update: updates.append(dict(update)))
    assert updates == []


# ---------------------------------------------------------------------------
# Batch transport (extraction.facts.transport: batch) — the SAME ingest
# chokepoint and gate contract as the sync tests above, driven through the
# Batches API instead. No network: `FakeBatchesAPI` stands in for
# `client.messages.batches`.
# ---------------------------------------------------------------------------


def _fake_message(text: str, *, usage: dict) -> object:
    block = type("_FBlock", (), {"type": "text", "text": text})()
    usage_obj = type(
        "_FUsage",
        (),
        {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        },
    )()
    return type("_FMessage", (), {"content": [block], "usage": usage_obj})()


def _fake_batch_result(
    custom_id: str, *, kind: str, message: object | None = None, error_type: str | None = None
) -> object:
    error = None
    if error_type is not None:
        error = type("_FError", (), {"type": error_type, "message": error_type})()
    result = type("_FResult", (), {"type": kind, "message": message, "error": error})()
    return type("_FItem", (), {"custom_id": custom_id, "result": result})()


class FakeBatchesAPI:
    """No-network double for ``client.messages.batches``.

    ``outcomes`` maps ``custom_id -> ("succeeded", reply_text) |
    ("errored", error_type) | ("canceled", None) | ("expired", None)`` — OR
    a LIST of such tuples, consumed in order across repeated ``create()``
    calls for the same custom_id (the seam a corrective-retry-batch test
    uses: the FIRST batch gets entry 0, the follow-up retry batch entry 1).
    A batch is ``ended`` immediately after ``create()`` unless ``hold()``
    is called for its id right after — the seam the resumability/deadline
    tests use to keep a batch ``in_progress`` until ``release()``.
    """

    def __init__(self, outcomes: dict | None = None, *, usage: dict | None = None) -> None:
        self.outcomes = outcomes or {}
        self.usage = usage or {"input_tokens": 1000, "output_tokens": 100}
        self.created: list[list] = []
        self.retrieved: list[str] = []
        self._next_id = 0
        self._held: set[str] = set()
        self._results: dict[str, list] = {}
        self._attempt: dict[str, int] = {}

    def create(self, *, requests):
        self._next_id += 1
        batch_id = f"batch_{self._next_id}"
        self.created.append(list(requests))
        results = []
        for req in requests:
            custom_id = req["custom_id"]
            spec = self.outcomes.get(custom_id, ("succeeded", "NODES\nEDGES\n"))
            if isinstance(spec, list):
                idx = min(self._attempt.get(custom_id, 0), len(spec) - 1)
                self._attempt[custom_id] = self._attempt.get(custom_id, 0) + 1
                kind, payload = spec[idx]
            else:
                kind, payload = spec
            if kind == "succeeded":
                results.append(
                    _fake_batch_result(custom_id, kind="succeeded", message=_fake_message(payload, usage=self.usage))
                )
            elif kind == "errored":
                results.append(_fake_batch_result(custom_id, kind="errored", error_type=payload or "overloaded_error"))
            else:
                results.append(_fake_batch_result(custom_id, kind=kind))
        self._results[batch_id] = results
        return type("_FBatch", (), {"id": batch_id})()

    def hold(self, batch_id: str) -> None:
        self._held.add(batch_id)

    def release(self, batch_id: str) -> None:
        self._held.discard(batch_id)

    def retrieve(self, batch_id: str):
        self.retrieved.append(batch_id)
        status = "in_progress" if batch_id in self._held else "ended"
        return type("_FBatch", (), {"id": batch_id, "processing_status": status})()

    def results(self, batch_id: str):
        # Reversed on purpose — the SDK's own contract is "any order", and
        # this proves the collector never assumes submission order.
        return iter(list(reversed(self._results.get(batch_id, []))))


class FakeBatchClient:
    def __init__(self, api: FakeBatchesAPI) -> None:
        self.messages = type("_FMessages", (), {"batches": api})()


def _run_batch(batch_client, **kwargs):
    from connectors.sharepoint.facts_extraction import run_facts_extraction

    return run_facts_extraction(CONNECTION_ID, transport="batch", batch_client=batch_client, **kwargs)


class _RefusingBatchesAPI:
    """A ``client.messages.batches`` double whose ``create()`` raises
    immediately — the live incident's own failure point ("every facts pass
    failed at batch submission")."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def create(self, *, requests):
        raise self._exc


def test_a_provider_limit_hit_at_batch_submission_ends_the_pass_cleanly(pg_env):
    """The batch transport's own version of the sync-transport test above —
    the live incident's exact shape: a workspace usage-limit exhaustion
    surfaces AT SUBMISSION, before any document-level result exists."""
    from connectors.sharepoint.facts_extraction import ProviderLimitHit
    from src.repositories import extraction_conditions_repo

    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="Some text.")

    api = _RefusingBatchesAPI(
        ProviderLimitHit("batch submission refused (workspace_limit)", reason="workspace_limit", retry_after_s=None)
    )
    report = _run_batch(FakeBatchClient(api))

    assert report["interrupted"] is True
    assert report["interrupted_reason"] == "provider_limit"
    conditions = extraction_conditions_repo().list_active()
    assert len(conditions) == 1
    assert conditions[0]["reason"] == "workspace_limit"
    # The Batches API is Anthropic-only — no Vertex region applies.
    assert conditions[0]["region"] == ""


def test_batch_pass_writes_claims_through_the_real_ingest_chokepoint(pg_env):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")
    _seed_document(file_id="cf_2", doc_id="doc2", text="Contoso Ltd signed in April.")

    node1 = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "rollout began in March"}],
    }
    node2 = {
        "id": "client:contoso-ltd",
        "type": "client",
        "attrs": {},
        "evidence": [{"doc_id": "doc2", "quote": "Contoso Ltd signed"}],
    }
    api = FakeBatchesAPI({"cf_1": ("succeeded", _stream(node1)), "cf_2": ("succeeded", _stream(node2))})
    report = _run_batch(FakeBatchClient(api))

    assert report["docs_extracted"] == 2
    assert report["docs_via_batch"] == 2
    assert report["docs_via_sync"] == 0
    assert report["claims_written"] == 2
    assert report["ingest_failures"] == []
    assert len(api.created) == 1, "both documents fit in one batch"

    from src.repositories import facts_repo

    found = facts_repo().search({"id": "admin1"}, type="engagement", filters={}, q=None, limit=10)
    assert found["subjects"], "the batch pass's facts must be readable back through the real search path"


def test_batch_gate_failure_defers_to_a_follow_up_retry_batch_by_default(pg_env):
    """``extraction.facts.retry_transport`` defaults to ``batch`` — a
    verbatim-gate failure must submit ONE follow-up batch (never a live
    sync call) and recover through the SAME merge rule `extract_one`'s own
    retry branch applies."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March for Contoso.")

    bad = {
        "id": "client:contoso",
        "type": "client",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "This sentence was never in the document."}],
    }
    fixed = {
        "id": "client:contoso",
        "type": "client",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "for Contoso"}],
    }
    api = FakeBatchesAPI({"cf_1": [("succeeded", _stream(bad)), ("succeeded", _stream(fixed))]})
    report = _run_batch(FakeBatchClient(api))

    assert len(api.created) == 2, "the gate failure must submit a follow-up retry batch"
    assert report["docs_extracted"] == 1
    assert report["claims_written"] == 1
    assert report["facts_retries"] == 1
    assert report["facts_quotes_dropped"] == 0
    assert report["docs_via_batch"] == 1

    from connectors.sharepoint.facts_extraction import load_state

    state = load_state(CONNECTION_ID)
    assert state["docs"]["cf_1"]["status"] == "done"


def test_batch_retry_batch_that_still_fails_is_dropped_and_counted(pg_env):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")

    bad = {
        "id": "client:x",
        "type": "client",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "never appeared"}],
    }
    still_bad = {
        "id": "client:x",
        "type": "client",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "still never appeared"}],
    }
    api = FakeBatchesAPI({"cf_1": [("succeeded", _stream(bad)), ("succeeded", _stream(still_bad))]})
    report = _run_batch(FakeBatchClient(api))

    assert len(api.created) == 2
    assert report["docs_extracted"] == 1, "the document is still processed — zero facts, not zero documents"
    assert report["claims_written"] == 0
    assert report["facts_quotes_dropped"] == 1
    assert report["facts_retries"] == 1


def test_batch_submission_records_batch_submitted_state_before_collection(pg_env):
    """The per-doc state entry a submission writes — checked mid-flight,
    before this pass's own collection step runs, by holding the batch
    `in_progress` for a moment via a second, isolated pass."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")

    api = FakeBatchesAPI({"cf_1": ("succeeded", _stream())})

    # Auto-hold every batch this test submits `in_progress` (the id is only
    # known after `create()`, so it can't be `hold()`ed ahead of time), and
    # expire the deadline on the SECOND check (Phase 1's submission gate
    # passes; Phase 2's first poll of the held batch sees it expired) — the
    # pass stops right after submission, before collection ever runs.
    real_create = api.create

    def create_and_hold(*, requests):
        batch = real_create(requests=requests)
        api.hold(batch.id)
        return batch

    api.create = create_and_hold  # type: ignore[method-assign]

    class ExpireAfterOne:
        def __init__(self) -> None:
            self.checks = 0

        def expired(self) -> bool:
            self.checks += 1
            return self.checks > 1

    report = _run_batch(FakeBatchClient(api), deadline=ExpireAfterOne())
    assert report["interrupted"] is True
    assert report["docs_extracted"] == 0

    from connectors.sharepoint.facts_extraction import load_state

    state = load_state(CONNECTION_ID)
    entry = state["docs"]["cf_1"]
    assert entry["status"] == "batch-submitted"
    assert entry["batch_id"] == "batch_1"
    assert entry["custom_id"] == "cf_1"
    assert entry["phase"] == "initial"
    assert "submitted_at" in entry


def test_batch_transient_error_requeues_and_a_later_pass_recovers(pg_env):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")

    api = FakeBatchesAPI({"cf_1": ("errored", "overloaded_error")})
    first = _run_batch(FakeBatchClient(api))
    assert first["docs_extracted"] == 0
    assert first["facts_failed"] == 0, "a transient error is requeued, not counted as a permanent failure"

    from connectors.sharepoint.facts_extraction import load_state

    state = load_state(CONNECTION_ID)
    assert "cf_1" not in state["docs"], "cleared back to plain pending so the next pass replans it"
    assert state["batch_attempts"]["cf_1"] == 1

    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "rollout began in March"}],
    }
    api2 = FakeBatchesAPI({"cf_1": ("succeeded", _stream(node))})
    second = _run_batch(FakeBatchClient(api2))
    assert second["docs_extracted"] == 1
    assert second["claims_written"] == 1


def test_batch_invalid_request_fails_without_consuming_an_attempt(pg_env):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")

    api = FakeBatchesAPI({"cf_1": ("errored", "invalid_request_error")})
    report = _run_batch(FakeBatchClient(api))
    assert report["facts_failed"] == 1
    assert report["docs_extracted"] == 0
    # Same `facts_failed_reasons` breakdown the sync transport's
    # `FactsDocumentError` handling records — one report shape regardless
    # of which transport actually ran.
    assert report["facts_failed_reasons"] == {"invalid_request": 1}

    from connectors.sharepoint.facts_extraction import load_state

    state = load_state(CONNECTION_ID)
    assert state["docs"]["cf_1"]["status"] == "failed"
    assert "invalid_request" in state["docs"]["cf_1"]["reason"]
    assert "cf_1" not in state.get("batch_attempts", {}), "a permanent failure never consumes a retry attempt"


def test_batch_deadline_mid_poll_leaves_state_resumable(pg_env):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")

    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "rollout began in March"}],
    }
    api = FakeBatchesAPI({"cf_1": ("succeeded", _stream(node))})

    class CountingDeadline:
        """Not expired for the FIRST check (Phase 1's submission gate);
        expired every check after — the FIRST poll of the freshly-submitted
        batch (which `FakeBatchesAPI` reports as `in_progress` while held)
        sees it, so the pass stops without ever sleeping."""

        def __init__(self) -> None:
            self.checks = 0

        def expired(self) -> bool:
            self.checks += 1
            return self.checks > 1

    # The batch id is only known after `create()`; submit once un-held to
    # learn it, then re-run against a client that holds it — instead,
    # simplest: hold ALL future batches by patching create to hold
    # immediately.
    real_create = api.create

    def create_and_hold(*, requests):
        batch = real_create(requests=requests)
        api.hold(batch.id)
        return batch

    api.create = create_and_hold  # type: ignore[method-assign]

    report = _run_batch(FakeBatchClient(api), deadline=CountingDeadline())
    assert report["interrupted"] is True
    assert report["interrupted_reason"] == "timeout"
    assert report["docs_extracted"] == 0

    from connectors.sharepoint.facts_extraction import load_state

    state = load_state(CONNECTION_ID)
    entry = state["docs"]["cf_1"]
    assert entry["status"] == "batch-submitted"
    batch_id = entry["batch_id"]

    # The batch finishes on Anthropic's side while we were between passes.
    api.release(batch_id)

    second = _run_batch(FakeBatchClient(api))
    assert second["docs_extracted"] == 1
    assert second["claims_written"] == 1
    assert len(api.created) == 1, "the second pass resumed the SAME batch — it never resubmitted"

    state2 = load_state(CONNECTION_ID)
    assert state2["docs"]["cf_1"]["status"] == "done"


def test_batch_resume_treats_a_29_day_old_entry_as_expired_without_a_network_call(pg_env):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")

    from datetime import timedelta

    from connectors.sharepoint.facts_extraction import BATCH_RESULT_RETENTION_DAYS, load_state, save_state

    stale_at = (datetime.now(timezone.utc) - timedelta(days=BATCH_RESULT_RETENTION_DAYS + 1)).isoformat(
        timespec="seconds"
    )
    state = load_state(CONNECTION_ID)
    state["docs"]["cf_1"] = {
        "status": "batch-submitted",
        "batch_id": "batch_ancient",
        "custom_id": "cf_1",
        "submitted_at": stale_at,
        "phase": "initial",
    }
    save_state(CONNECTION_ID, state)

    api = FakeBatchesAPI({})
    first = _run_batch(FakeBatchClient(api))
    assert first["docs_extracted"] == 0
    assert api.created == [], "the stale entry never becomes a Phase 1 submission this pass"
    assert "batch_ancient" not in api.retrieved, "expired-by-age is checked BEFORE any network call"

    state_after = load_state(CONNECTION_ID)
    assert "cf_1" not in state_after["docs"], "requeued back to plain pending for the NEXT pass to resubmit"

    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "rollout began in March"}],
    }
    api2 = FakeBatchesAPI({"cf_1": ("succeeded", _stream(node))})
    second = _run_batch(FakeBatchClient(api2))
    assert second["docs_extracted"] == 1


def test_batch_cost_uses_the_batch_price_multiplier(pg_env):
    from src.llm_pricing import cost_usd

    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")

    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "rollout began in March"}],
    }
    api = FakeBatchesAPI({"cf_1": ("succeeded", _stream(node))}, usage={"input_tokens": 2000, "output_tokens": 300})
    report = _run_batch(FakeBatchClient(api))

    expected = cost_usd(model=report["model"], input_tokens=2000, output_tokens=300, batch=True)
    assert report["facts_usage"]["estimated_cost_usd"] == round(expected, 4)
    assert report["facts_usage"]["input_tokens"] == 2000
    assert report["facts_usage"]["output_tokens"] == 300


def test_batch_default_transport_is_still_sync(pg_env):
    """No behavior change for a caller that never opts in —
    extraction.facts.transport defaults to sync."""
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="The Northwind rollout began in March.")

    node = {
        "id": "engagement:northwind-rollout",
        "type": "engagement",
        "attrs": {},
        "evidence": [{"doc_id": "doc1", "quote": "rollout began in March"}],
    }
    report = _run(StubExtractor([_stream(node)]))
    assert report["docs_via_batch"] == 0
    assert report["docs_via_sync"] == 1


# ---------------------------------------------------------------------------
# Auto-continuation (TCRD-296 gap #61): a pass that stopped on its own time
# budget with documents still pending re-enqueues itself.
# ---------------------------------------------------------------------------


def _seed_four_documents() -> None:
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    for i in range(4):
        _seed_document(file_id=f"cf_{i}", doc_id=f"doc{i}", text=f"The rollout number {i} began in March.")


class _ExpireAfterOne:
    """Expires once one document has been submitted — same shape as
    ``test_an_expired_deadline_stops_between_documents_and_keeps_what_it_paid_for``'s
    own fixture above."""

    def __init__(self) -> None:
        self.checks = 0

    @property
    def expired(self) -> bool:
        self.checks += 1
        return self.checks > 1


def _reply_for(message: str) -> str:
    doc_id = json.loads(message.split("```json\n", 1)[1].split("\n```", 1)[0])["doc_id"]
    index = doc_id.removeprefix("doc")
    return _stream(
        {
            "id": f"engagement:rollout-{index}",
            "type": "engagement",
            "attrs": {},
            "evidence": [{"doc_id": doc_id, "quote": f"rollout number {index} began in March"}],
        }
    )


class _ScriptedExtractor(StubExtractor):
    def call(self, user_message: str) -> str:
        self.seen.append(user_message)
        self.usage["calls"] += 1
        return _reply_for(user_message)


def _mark_job_done(job_id: str) -> None:
    """Force a QUEUED job straight to 'done' — bypassing the claim/lease
    lifecycle (already covered elsewhere) so a chain-cap test doesn't have
    to wait out each continuation's real `run_after` delay."""
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(sa.text("UPDATE jobs SET status = 'done' WHERE id = :id"), {"id": job_id})


def test_count_pending_documents_counts_never_attempted_documents(pg_env):
    from connectors.sharepoint.facts_extraction import count_pending_documents

    _seed_four_documents()
    assert count_pending_documents(CONNECTION_ID) == 4


def test_count_pending_documents_excludes_done_documents(pg_env):
    from connectors.sharepoint.facts_extraction import count_pending_documents

    _seed_four_documents()
    _run(_ScriptedExtractor([]), concurrency=1)
    assert count_pending_documents(CONNECTION_ID) == 0


def test_count_pending_documents_counts_what_a_timed_out_pass_left_behind(pg_env):
    from connectors.sharepoint.facts_extraction import count_pending_documents

    _seed_four_documents()
    report = _run(_ScriptedExtractor([]), concurrency=1, deadline=_ExpireAfterOne())
    assert report["interrupted_reason"] == "timeout"
    assert count_pending_documents(CONNECTION_ID) == 3


def test_count_pending_documents_excludes_a_terminal_skip_state(pg_env):
    from connectors.sharepoint.facts_extraction import count_pending_documents, load_state

    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(file_id="cf_1", doc_id="doc1", text="")  # no chunk text -> skipped-no-text

    _run(_ScriptedExtractor([]), concurrency=1)

    assert load_state(CONNECTION_ID)["docs"]["cf_1"]["status"] == "skipped-no-text"
    assert count_pending_documents(CONNECTION_ID) == 0


def test_count_pending_documents_is_zero_for_unknown_connection(pg_env):
    from connectors.sharepoint.facts_extraction import count_pending_documents

    assert count_pending_documents("does-not-exist") == 0


def test_count_pending_documents_is_zero_with_no_ontology(pg_env):
    from connectors.sharepoint.facts_extraction import count_pending_documents

    _seed_collection()
    _seed_connection()
    _seed_document(file_id="cf_1", doc_id="doc1", text="hello")
    assert count_pending_documents(CONNECTION_ID) == 0


def test_maybe_continue_pass_enqueues_a_delayed_job_carrying_the_run_options(pg_env):
    from connectors.sharepoint.facts_extraction import (
        FACTS_CONTINUATION_DELAY_S,
        facts_extraction_idempotency_key,
        maybe_continue_pass,
    )
    from src.repositories import jobs_repo

    _seed_four_documents()
    report = _run(_ScriptedExtractor([]), concurrency=1, deadline=_ExpireAfterOne())
    assert report["interrupted_reason"] == "timeout"

    before = datetime.now(timezone.utc)
    new_job_id = maybe_continue_pass(
        CONNECTION_ID,
        payload={"connection_id": CONNECTION_ID, "doc_ids": ["doc1"], "timeout_s": 900},
        report=report,
        original_job_id="orig-job-1",
    )
    assert new_job_id is not None

    job = jobs_repo().get(new_job_id)
    assert job["kind"] == "sharepoint-facts-extraction"
    assert job["idempotency_key"] == facts_extraction_idempotency_key(CONNECTION_ID)
    assert job["status"] == "queued"
    assert job["payload_json"] == {
        "connection_id": CONNECTION_ID,
        "doc_ids": ["doc1"],
        "timeout_s": 900,
        "continued_from": "orig-job-1",
    }
    run_after = job["run_after"]
    if run_after.tzinfo is None:
        run_after = run_after.replace(tzinfo=timezone.utc)
    delta_s = (run_after - before).total_seconds()
    assert FACTS_CONTINUATION_DELAY_S - 5 <= delta_s <= FACTS_CONTINUATION_DELAY_S + 15


def test_maybe_continue_pass_does_nothing_for_a_non_timeout_reason(pg_env):
    from connectors.sharepoint.facts_extraction import maybe_continue_pass
    from src.repositories import jobs_repo

    _seed_four_documents()
    result = maybe_continue_pass(
        CONNECTION_ID,
        payload={"connection_id": CONNECTION_ID},
        report={"interrupted": False, "interrupted_reason": None},
        original_job_id="orig-job-1",
    )
    assert result is None
    assert jobs_repo().list(kind="sharepoint-facts-extraction", status="queued", limit=10) == []


def test_maybe_continue_pass_does_not_chain_onto_a_provider_limit_stop(pg_env):
    """A provider refusal (TCRD-296 synthesis F.25) reports
    ``interrupted_reason: "provider_limit"``, never ``"timeout"`` — the
    self-continuation chain must not re-enqueue into the SAME condition
    that just stopped the pass; the crawl's OWN streamed trigger is what
    stays suppressed while the condition is active
    (``streamed_pass_suppressed_by_provider_limit``, exercised in
    ``tests/db_pg/test_extraction_conditions_pg.py``)."""
    from connectors.sharepoint.facts_extraction import maybe_continue_pass
    from src.repositories import jobs_repo

    _seed_four_documents()
    result = maybe_continue_pass(
        CONNECTION_ID,
        payload={"connection_id": CONNECTION_ID},
        report={"interrupted": True, "interrupted_reason": "provider_limit"},
        original_job_id="orig-job-1",
    )
    assert result is None
    assert jobs_repo().list(kind="sharepoint-facts-extraction", status="queued", limit=10) == []


def test_maybe_continue_pass_does_nothing_when_nothing_is_pending(pg_env):
    from connectors.sharepoint.facts_extraction import maybe_continue_pass
    from src.repositories import jobs_repo

    _seed_four_documents()
    _run(_ScriptedExtractor([]), concurrency=1)  # fully drains the corpus

    result = maybe_continue_pass(
        CONNECTION_ID,
        payload={"connection_id": CONNECTION_ID},
        report={"interrupted": True, "interrupted_reason": "timeout"},
        original_job_id="orig-job-1",
    )
    assert result is None
    assert jobs_repo().list(kind="sharepoint-facts-extraction", status="queued", limit=10) == []


def test_maybe_continue_pass_stamps_continued_by_job_id_on_the_original(pg_env):
    from connectors.sharepoint.facts_extraction import maybe_continue_pass
    from src.repositories import jobs_repo

    _seed_four_documents()
    jobs_repo().enqueue("sharepoint-facts-extraction", {"connection_id": CONNECTION_ID})
    claimed = jobs_repo().claim_next(kinds=["sharepoint-facts-extraction"], worker_id="w1")
    report = {"interrupted": True, "interrupted_reason": "timeout"}
    jobs_repo().complete(claimed["id"], "w1", claimed["lease_token"], report)

    new_job_id = maybe_continue_pass(
        CONNECTION_ID, payload={"connection_id": CONNECTION_ID}, report=report, original_job_id=claimed["id"]
    )

    assert new_job_id is not None
    original = jobs_repo().get(claimed["id"])
    assert original["payload_json"]["result"]["continued_by_job_id"] == new_job_id
    assert original["payload_json"]["result"]["interrupted_reason"] == "timeout"


def test_maybe_continue_pass_stops_at_the_chain_cap(pg_env):
    from connectors.sharepoint.facts_extraction import MAX_CONSECUTIVE_FACTS_CONTINUATIONS, maybe_continue_pass

    _seed_four_documents()
    report = {"interrupted": True, "interrupted_reason": "timeout"}

    for _ in range(MAX_CONSECUTIVE_FACTS_CONTINUATIONS):
        job_id = maybe_continue_pass(
            CONNECTION_ID, payload={"connection_id": CONNECTION_ID}, report=report, original_job_id=None
        )
        assert job_id is not None
        _mark_job_done(job_id)

    refused = maybe_continue_pass(
        CONNECTION_ID, payload={"connection_id": CONNECTION_ID}, report=report, original_job_id=None
    )
    assert refused is None


def test_maybe_continue_pass_resets_the_chain_once_a_pass_drains_the_backlog(pg_env):
    from connectors.sharepoint.facts_extraction import load_state, maybe_continue_pass

    _seed_four_documents()
    report = {"interrupted": True, "interrupted_reason": "timeout"}

    job_id = maybe_continue_pass(
        CONNECTION_ID, payload={"connection_id": CONNECTION_ID}, report=report, original_job_id=None
    )
    assert job_id is not None
    assert load_state(CONNECTION_ID)["facts_continuation_chain"] == 1
    _mark_job_done(job_id)

    _run(_ScriptedExtractor([]), concurrency=1)  # drains the whole backlog

    result = maybe_continue_pass(
        CONNECTION_ID, payload={"connection_id": CONNECTION_ID}, report=report, original_job_id=None
    )
    assert result is None
    assert load_state(CONNECTION_ID)["facts_continuation_chain"] == 0


# ---------------------------------------------------------------------------
# Partitioned passes (TCRD-296 gap #67)
# ---------------------------------------------------------------------------


def test_merge_docs_is_a_per_document_upsert_not_a_whole_payload_overwrite(pg_env):
    """The crux: two writers persisting DIFFERENT document keys through
    ``state_store.merge_docs`` must both survive — the bug a whole-payload
    ``save_state`` overwrite would reproduce (the second writer's stale
    in-memory snapshot of the FIRST writer's key would clobber it)."""
    from connectors.sharepoint.facts_extraction import load_state
    from connectors.sharepoint.state_store import merge_docs

    _seed_collection()
    _seed_connection()
    _seed_ontology()

    merge_docs("facts", CONNECTION_ID, set_entries={"cf_1": {"status": "done", "extracted_sha": "a"}}, removed=[])
    merge_docs("facts", CONNECTION_ID, set_entries={"cf_2": {"status": "done", "extracted_sha": "b"}}, removed=[])

    docs = load_state(CONNECTION_ID)["docs"]
    assert docs["cf_1"] == {"status": "done", "extracted_sha": "a"}
    assert docs["cf_2"] == {"status": "done", "extracted_sha": "b"}


def test_merge_docs_removal_touches_only_the_named_keys(pg_env):
    from connectors.sharepoint.facts_extraction import load_state
    from connectors.sharepoint.state_store import merge_docs

    _seed_collection()
    _seed_connection()
    _seed_ontology()

    merge_docs(
        "facts",
        CONNECTION_ID,
        set_entries={"cf_1": {"status": "done"}, "cf_2": {"status": "done"}},
        removed=[],
    )
    merge_docs("facts", CONNECTION_ID, set_entries={}, removed=["cf_1"])

    docs = load_state(CONNECTION_ID)["docs"]
    assert "cf_1" not in docs
    assert docs["cf_2"] == {"status": "done"}


def test_two_sequential_partitions_both_persist_without_clobbering_each_other(pg_env):
    """End to end: two partitions of the SAME connection's facts pass, run
    one after the other (each partition's in-memory state was loaded
    BEFORE the other's own writes — the exact staleness a whole-payload
    ``save_state`` would clobber on), both leave their own documents
    ``done`` in the shared ledger."""
    from connectors.sharepoint.facts_extraction import _partition_of, load_state

    _seed_collection()
    _seed_connection()
    _seed_ontology()
    for i in range(6):
        _seed_document(file_id=f"cf_{i}", doc_id=f"doc{i}", text=f"The rollout number {i} began in March.")

    count = 2
    owned = {index: [f"cf_{i}" for i in range(6) if _partition_of(f"cf_{i}", count) == index] for index in range(count)}
    assert owned[0] and owned[1]  # both partitions actually own something

    _run(_ScriptedExtractor([]), concurrency=1, partition=(0, count))
    _run(_ScriptedExtractor([]), concurrency=1, partition=(1, count))

    docs = load_state(CONNECTION_ID)["docs"]
    for file_id in owned[0] + owned[1]:
        assert docs[file_id]["status"] == "done", (file_id, docs.get(file_id))


def test_a_partitioned_pass_only_ever_touches_its_own_documents(pg_env):
    from connectors.sharepoint.facts_extraction import _partition_of, load_state

    _seed_collection()
    _seed_connection()
    _seed_ontology()
    for i in range(6):
        _seed_document(file_id=f"cf_{i}", doc_id=f"doc{i}", text=f"The rollout number {i} began in March.")

    count = 2
    owned0 = [f"cf_{i}" for i in range(6) if _partition_of(f"cf_{i}", count) == 0]
    owned1 = [f"cf_{i}" for i in range(6) if _partition_of(f"cf_{i}", count) == 1]
    assert owned0 and owned1

    _run(_ScriptedExtractor([]), concurrency=1, partition=(0, count))

    docs = load_state(CONNECTION_ID)["docs"]
    for file_id in owned0:
        assert file_id in docs
    for file_id in owned1:
        assert file_id not in docs  # untouched by partition 0


def test_enqueue_facts_extraction_passes_fans_out_by_pending_backlog(pg_env, monkeypatch):
    from connectors.sharepoint.facts_extraction import enqueue_facts_extraction_passes
    from src.repositories import jobs_repo

    _seed_four_documents()
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *path, default=None: 4 if path[-1] == "concurrency_passes" else default,
    )

    jobs = enqueue_facts_extraction_passes(CONNECTION_ID, pending=8001)  # ceil(8001/2000) = 5, capped at 4
    assert len(jobs) == 4
    keys = {job["idempotency_key"] for job in jobs}
    assert len(keys) == 4  # every partition gets its own key
    live = jobs_repo().list(kind="sharepoint-facts-extraction", status="queued", limit=10)
    assert len(live) == 4
    partitions = sorted(job["payload_json"]["partition"]["index"] for job in live)
    assert partitions == [0, 1, 2, 3]
    for job in live:
        assert job["payload_json"]["partition"]["count"] == 4


def test_enqueue_facts_extraction_passes_second_call_is_a_no_op(pg_env, monkeypatch):
    from connectors.sharepoint.facts_extraction import enqueue_facts_extraction_passes
    from src.repositories import jobs_repo

    _seed_four_documents()
    first = enqueue_facts_extraction_passes(CONNECTION_ID, pending=5000)
    second = enqueue_facts_extraction_passes(CONNECTION_ID, pending=5000)
    assert [j["id"] for j in first] == [j["id"] for j in second]
    assert all(j.get("deduped") for j in second)
    live = jobs_repo().list(kind="sharepoint-facts-extraction", status="queued", limit=50)
    assert len(live) == len(first)  # nothing piled up


def test_enqueue_facts_extraction_passes_with_no_backlog_is_a_single_legacy_shaped_job(pg_env):
    """count==1 (no real backlog) keeps today's payload/key shape byte for
    byte — no ``partition`` key at all — so every pre-existing caller/test
    of the singular trigger stays unaffected."""
    from connectors.sharepoint.facts_extraction import enqueue_facts_extraction_passes, facts_extraction_idempotency_key

    _seed_four_documents()
    jobs = enqueue_facts_extraction_passes(CONNECTION_ID, pending=0)
    assert len(jobs) == 1
    assert jobs[0]["idempotency_key"] == facts_extraction_idempotency_key(CONNECTION_ID)
    assert "partition" not in jobs[0]["payload_json"]


def test_any_facts_pass_running_sees_a_held_partition_lock(pg_env):
    from connectors.sharepoint.state_store import any_facts_pass_running, facts_pass_lock

    _seed_collection()
    _seed_connection()

    assert any_facts_pass_running(CONNECTION_ID) is False
    with facts_pass_lock(CONNECTION_ID, partition=(1, 4)):
        assert any_facts_pass_running(CONNECTION_ID) is True
    assert any_facts_pass_running(CONNECTION_ID) is False


def test_two_different_partition_indices_can_run_at_once(pg_env):
    from connectors.sharepoint.state_store import facts_pass_lock

    _seed_collection()
    _seed_connection()

    with facts_pass_lock(CONNECTION_ID, partition=(0, 4)):
        with facts_pass_lock(CONNECTION_ID, partition=(1, 4)):
            pass  # no FactsPassLocked — distinct partitions never contend


def test_the_same_partition_index_cannot_run_twice_at_once(pg_env):
    from connectors.sharepoint.state_store import FactsPassLocked, facts_pass_lock

    _seed_collection()
    _seed_connection()

    with facts_pass_lock(CONNECTION_ID, partition=(2, 4)):
        with pytest.raises(FactsPassLocked):
            with facts_pass_lock(CONNECTION_ID, partition=(2, 4)):
                pass


def test_reset_no_claims_refuses_while_a_partition_is_running(pg_env):
    from connectors.sharepoint.facts_extraction import reset_no_claims_ledger_entries
    from connectors.sharepoint.state_store import FactsPassLocked, facts_pass_lock

    _seed_collection()
    _seed_connection()

    with facts_pass_lock(CONNECTION_ID, partition=(3, 4)):
        with pytest.raises(FactsPassLocked):
            reset_no_claims_ledger_entries(CONNECTION_ID)


def test_a_multi_partition_pass_sweeps_orphans_only_once_the_last_partition_finishes(pg_env, monkeypatch):
    """#2220's end-of-pass sweep must fire once per CONNECTION, after the
    LAST partition of a generation finishes — never once per partition."""
    from src.repositories import jobs_repo

    _seed_collection()
    _seed_connection()
    _seed_ontology()
    for i in range(4):
        _seed_document(file_id=f"cf_{i}", doc_id=f"doc{i}", text=f"The rollout number {i} began in March.")

    sweep_calls: list[str] = []
    monkeypatch.setattr(
        "connectors.sharepoint.facts_extraction._run_end_of_pass_orphan_sweep",
        lambda report: sweep_calls.append("swept"),
    )

    count = 2
    # Partition 1 is still "queued" while partition 0 runs — so partition
    # 0 finishing must NOT sweep yet.
    jobs_repo().enqueue(
        "sharepoint-facts-extraction",
        {"connection_id": CONNECTION_ID, "partition": {"index": 1, "count": count}},
        idempotency_key="sharepoint-facts-extraction:%s:1/%d" % (CONNECTION_ID, count),
    )
    _run(_ScriptedExtractor([]), concurrency=1, partition=(0, count))
    assert sweep_calls == []

    # Now no sibling is queued/running any more — the LAST partition sweeps.
    jobs_repo().list(kind="sharepoint-facts-extraction", status="queued", limit=10)
    from src.db_pg import get_engine
    import sqlalchemy as sa2

    with get_engine().begin() as conn:
        conn.execute(sa2.text("UPDATE jobs SET status = 'done' WHERE kind = 'sharepoint-facts-extraction'"))

    _run(_ScriptedExtractor([]), concurrency=1, partition=(1, count))
    assert sweep_calls == ["swept"]
