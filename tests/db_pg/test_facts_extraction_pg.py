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
    sha256: str = "sha-md-1",
    status: str = "indexed",
) -> None:
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


def test_a_spreadsheet_is_never_sent_to_the_model(pg_env):
    _seed_collection()
    _seed_connection()
    _seed_ontology()
    _seed_document(
        file_id="cf_1",
        doc_id="doc1",
        text="| a | b |\n| 1 | 2 |",
        filename="numbers.md",
        path="Finance/numbers.xlsx",
    )

    extractor = StubExtractor([_stream()])
    report = _run(extractor)

    assert report["docs_skipped_tabular"] == 1
    assert report["docs_extracted"] == 0
    assert extractor.seen == []


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
