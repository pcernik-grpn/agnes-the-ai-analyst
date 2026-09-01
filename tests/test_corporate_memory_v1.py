"""Tests for Corporate Memory V1: verification detector, confidence, contradiction, entities.

Three-tier testing approach:
- Tier 1: Unit tests (no LLM, no mocking) — schema, parsing, confidence math, entity matching
- Tier 2: Integration tests (mocked LLM) — full pipelines with golden file responses
- Tier 3: Live LLM tests (CI-skippable) — marked with @pytest.mark.live_llm
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SESSIONS_DIR = FIXTURES_DIR / "sessions"
VERIFICATIONS_DIR = FIXTURES_DIR / "verifications"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_db(tmp_path, monkeypatch):
    """Create a fresh DuckDB with the latest schema."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Force re-creation of shared connection
    import src.db as db_module

    db_module._system_db_conn = None
    db_module._system_db_path = None
    conn = db_module.get_system_db()
    return conn


def _run_verification_processor(conn, extractor, session_data_dir=None):
    """Run the verification processor through the new framework.

    Returns a stats dict with both new keys (scanned/processed/skipped/
    items_extracted) AND legacy aliases (sessions_scanned/sessions_processed/
    sessions_skipped/verifications_extracted/items_created/contradictions_recorded)
    derived from pre/post row counts so existing assertions keep working
    after the session-pipeline refactor.
    """
    from services.session_pipeline.runner import run_processor
    from services.session_processors.verification import VerificationProcessor

    pre_evidence = conn.execute("SELECT COUNT(*) FROM verification_evidence").fetchone()[0]
    pre_contradictions = conn.execute("SELECT COUNT(*) FROM knowledge_contradictions").fetchone()[0]

    processor = VerificationProcessor(extractor)
    stats = run_processor(conn, processor, session_data_dir=session_data_dir)

    post_evidence = conn.execute("SELECT COUNT(*) FROM verification_evidence").fetchone()[0]
    post_contradictions = conn.execute("SELECT COUNT(*) FROM knowledge_contradictions").fetchone()[0]

    return {
        **stats,
        "sessions_scanned": stats["scanned"],
        "sessions_processed": stats["processed"],
        "sessions_skipped": stats["skipped"],
        "verifications_extracted": post_evidence - pre_evidence,
        "items_created": stats["items_extracted"],
        "contradictions_recorded": post_contradictions - pre_contradictions,
    }


def _load_golden(name: str) -> dict:
    """Load a golden verification output file."""
    with open(VERIFICATIONS_DIR / f"{name}.json") as f:
        return json.load(f)


def _mock_extractor(golden_response: dict) -> MagicMock:
    """Create a mock StructuredExtractor that returns a golden response."""
    mock = MagicMock()
    mock.extract_json.return_value = golden_response
    return mock


# ===========================================================================
# TIER 1: Unit Tests (no LLM)
# ===========================================================================


class TestSchemaV8Migration:
    """Test DuckDB schema v7 -> v8 migration."""

    def test_fresh_install_has_v8_tables(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
        assert "knowledge_contradictions" in tables
        # v29 renamed session_extraction_state → session_processor_state with
        # composite (processor_name, session_file) PK so multiple processors
        # can track their own processed-set independently.
        assert "session_processor_state" in tables
        assert "session_extraction_state" not in tables
        conn.close()

    def test_knowledge_items_has_new_columns(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'knowledge_items'"
            ).fetchall()
        }
        # v49: ``domain`` scalar column dropped; v8 metadata is otherwise intact.
        # The junction lives in ``knowledge_item_domains`` now.
        new_columns = {
            "confidence",
            "entities",
            "source_type",
            "source_ref",
            "valid_from",
            "valid_until",
            "supersedes",
            "sensitivity",
            "is_personal",
        }
        assert new_columns.issubset(columns), f"Missing: {new_columns - columns}"
        assert "domain" not in columns, "v49 dropped knowledge_items.domain scalar"
        conn.close()

    def test_schema_version_matches_constant(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.db import SCHEMA_VERSION, get_schema_version

        assert get_schema_version(conn) == SCHEMA_VERSION
        conn.close()

    def test_verification_evidence_table_exists(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
        assert "verification_evidence" in tables
        conn.close()


class TestKnowledgeRepositoryV1:
    """Test extended KnowledgeRepository with V1 fields."""

    def test_create_with_new_fields(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)

        repo.create(
            id="kv_test001",
            title="Test item",
            content="Test content",
            category="business_logic",
            source_user="analyst@test.com",
            confidence=0.90,
            domain="finance",
            entities=["churn", "MRR"],
            source_type="user_verification",
            source_ref="session-2026-04-22-analyst",
            sensitivity="internal",
        )

        item = repo.get_by_id("kv_test001")
        assert item is not None
        assert item["confidence"] == 0.90
        assert item["domain"] == "finance"
        assert item["source_type"] == "user_verification"
        assert item["source_ref"] == "session-2026-04-22-analyst"
        assert item["is_personal"] is False
        conn.close()

    def test_list_by_domain(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)

        repo.create(id="k1", title="A", content="a", category="x", domain="finance")
        repo.create(id="k2", title="B", content="b", category="x", domain="engineering")
        repo.create(id="k3", title="C", content="c", category="x", domain="finance")

        finance_items = repo.list_by_domain("finance")
        assert len(finance_items) == 2
        assert all(i["domain"] == "finance" for i in finance_items)
        conn.close()

    def test_set_personal_flag(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)

        repo.create(id="k1", title="A", content="a", category="x", source_user="me@test.com")
        repo.set_personal("k1", True)
        item = repo.get_by_id("k1")
        assert item["is_personal"] is True

        repo.set_personal("k1", False)
        item = repo.get_by_id("k1")
        assert item["is_personal"] is False
        conn.close()

    def test_exclude_personal_from_list(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)

        repo.create(id="k1", title="Public", content="a", category="x", is_personal=False)
        repo.create(id="k2", title="Personal", content="b", category="x", is_personal=True)

        all_items = repo.list_items(exclude_personal=False)
        assert len(all_items) == 2

        public_only = repo.list_items(exclude_personal=True)
        assert len(public_only) == 1
        assert public_only[0]["id"] == "k1"
        conn.close()

    def test_user_contributions(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)

        repo.create(id="k1", title="A", content="a", category="x", source_user="alice@test.com")
        repo.create(id="k2", title="B", content="b", category="x", source_user="bob@test.com")
        repo.create(id="k3", title="C", content="c", category="x", source_user="alice@test.com")

        alice_items = repo.get_user_contributions("alice@test.com")
        assert len(alice_items) == 2
        conn.close()

    def test_contradiction_crud(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)

        cid = repo.create_contradiction(
            item_a_id="k1",
            item_b_id="k2",
            explanation="They disagree on churn definition",
            severity="hard",
            suggested_resolution="k1 is more recent and verified",
        )
        assert cid.startswith("kc_")

        contradictions = repo.list_contradictions(resolved=False)
        assert len(contradictions) == 1
        assert contradictions[0]["item_a_id"] == "k1"

        repo.resolve_contradiction(cid, "admin@test.com", "kept_a")
        resolved = repo.list_contradictions(resolved=True)
        assert len(resolved) == 1
        assert resolved[0]["resolution"] == "kept_a"
        conn.close()

    def test_session_processor_state(self, tmp_path, monkeypatch):
        """Post-v29: session-processed bookkeeping moved out of
        KnowledgeRepository into SessionProcessorStateRepository, keyed by
        (processor_name, session_file). Each processor tracks its own
        processed-set independently."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.session_processor_state import SessionProcessorStateRepository

        repo = SessionProcessorStateRepository(conn)

        assert repo.is_processed("verification", "alice/session1.jsonl", "abc123") is False

        repo.mark_processed("verification", "alice/session1.jsonl", "alice", 3, "abc123")
        assert repo.is_processed("verification", "alice/session1.jsonl", "abc123") is True
        # Different hash → treated as unprocessed (live append invalidation).
        assert repo.is_processed("verification", "alice/session1.jsonl", "different") is False
        # Another session not seen at all.
        assert repo.is_processed("verification", "alice/session2.jsonl", "any") is False
        # Different processor → independent state.
        assert repo.is_processed("usage", "alice/session1.jsonl", "abc123") is False
        conn.close()

    def test_find_contradiction_candidates(self, tmp_path, monkeypatch):
        """Domain-only narrowing — topic matching is delegated to the LLM judge
        in services.corporate_memory.contradiction.find_and_judge (ADR D4)."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)

        repo.create(
            id="k1",
            title="Churn is revenue-based",
            content="MRR churn",
            category="x",
            domain="finance",
            status="approved",
        )
        repo.create(
            id="k2",
            title="NPS calculation",
            content="Rolling 90 day",
            category="x",
            domain="product",
            status="approved",
        )
        repo.create(
            id="k3",
            title="Churn is customer-count based",
            content="Customer churn",
            category="x",
            domain="finance",
            status="approved",
        )

        candidates = repo.find_contradiction_candidates(
            new_item_id="k_new",
            domain="finance",
        )
        ids = {c["id"] for c in candidates}
        # Only same-domain candidates are surfaced; the LLM does the rest.
        assert ids == {"k1", "k3"}
        conn.close()


class TestConfidenceScoring:
    """Test confidence scoring module (pure math, no LLM)."""

    def test_correction_base_confidence(self):
        from services.corporate_memory.confidence import compute_confidence

        c = compute_confidence("user_verification", "correction")
        assert c == 0.90

    def test_confirmation_base_confidence(self):
        from services.corporate_memory.confidence import compute_confidence

        c = compute_confidence("user_verification", "confirmation")
        assert c == 0.60

    def test_unprompted_definition_base_confidence(self):
        from services.corporate_memory.confidence import compute_confidence

        c = compute_confidence("user_verification", "unprompted_definition")
        assert c == 0.90

    def test_admin_mandate_always_1(self):
        from services.corporate_memory.confidence import compute_confidence

        c = compute_confidence("admin_mandate")
        assert c == 1.00

    def test_claude_local_md_base(self):
        from services.corporate_memory.confidence import compute_confidence

        c = compute_confidence("claude_local_md")
        assert c == 0.50

    def test_multi_user_boost(self):
        from services.corporate_memory.confidence import boost_for_multi_verification

        # 2 additional verifiers = +0.10
        c = boost_for_multi_verification(0.90, verification_count=3)
        assert c == pytest.approx(1.00)  # 0.90 + 0.05*2 = 1.00

    def test_boost_capped_at_max(self):
        from services.corporate_memory.confidence import boost_for_multi_verification

        c = boost_for_multi_verification(0.95, verification_count=10)
        assert c == 1.00

    def test_decay_over_time(self):
        from services.corporate_memory.confidence import apply_decay

        created = datetime.now(timezone.utc) - timedelta(days=60)  # ~2 months
        # exponential: 0.90 * (0.5 ** (2/12)) ≈ 0.90 * 0.891 ≈ 0.802
        c = apply_decay(0.90, created)
        assert c < 0.90
        assert c == pytest.approx(0.90 * (0.5 ** (2.0 / 12.0)), abs=0.01)

    def test_decay_never_below_floor(self):
        from services.corporate_memory.confidence import apply_decay

        created = datetime.now(timezone.utc) - timedelta(days=3650)  # 10 years
        c = apply_decay(0.50, created)
        assert c >= 0.0

    def test_admin_mandate_decay_floor(self):
        from services.corporate_memory.confidence import apply_decay

        created = datetime.now(timezone.utc) - timedelta(days=3650)  # 10 years
        c = apply_decay(1.00, created, source_type="admin_mandate")
        assert c >= 0.50  # admin_mandate floor is 0.50

    def test_configure_overrides_defaults(self):
        import copy
        from services.corporate_memory import confidence as cm

        original_base = dict(cm._BASE_CONFIDENCE)
        original_decay = copy.deepcopy(cm._DECAY_CONFIG)
        try:
            cm.configure(
                {
                    "base": {
                        "user_verification.correction": 0.75,
                    },
                    "decay": {
                        "mode": "exponential",
                        "half_life_months": 6,
                        "floor": {"admin_mandate": 0.60, "default": 0.0},
                    },
                }
            )
            c = cm.compute_confidence("user_verification", "correction")
            assert c == pytest.approx(0.75)
            created = datetime.now(timezone.utc) - timedelta(days=365)  # 12 months
            # exponential with half_life=6: 1.00 * (0.5 ** (12/6)) = 0.25, but floor=0.60
            c2 = cm.apply_decay(1.00, created, source_type="admin_mandate")
            assert c2 >= 0.60
        finally:
            cm._BASE_CONFIDENCE = original_base
            cm._DECAY_CONFIG.clear()
            cm._DECAY_CONFIG.update(original_decay)


class TestEntityResolution:
    """Test entity resolution v1 (string matching, no LLM)."""

    def test_basic_matching(self):
        from services.corporate_memory.entities import resolve_entities

        registry = {
            "metrics": ["churn", "MRR", "ARR"],
            "teams": ["engineering", "finance"],
        }
        matches = resolve_entities(
            content="Our churn metric uses MRR data from the finance team",
            title="Churn definition",
            entity_registry=registry,
        )
        assert "churn" in matches
        assert "MRR" in matches
        assert "finance" in matches
        assert "ARR" not in matches

    def test_case_insensitive(self):
        from services.corporate_memory.entities import resolve_entities

        registry = {"metrics": ["NPS"]}
        matches = resolve_entities(
            content="Our nps score is tracked weekly",
            title="NPS Tracking",
            entity_registry=registry,
        )
        assert "NPS" in matches

    def test_empty_registry(self):
        from services.corporate_memory.entities import resolve_entities

        matches = resolve_entities("some content", "some title", {})
        assert matches == []

    def test_resolve_and_merge(self):
        from services.corporate_memory.entities import resolve_and_merge

        registry = {"metrics": ["churn", "MRR"]}
        item = {
            "title": "Churn definition",
            "content": "Uses MRR data",
            "entities": ["existing_entity"],
        }
        merged = resolve_and_merge(item, registry)
        assert "existing_entity" in merged
        assert "churn" in merged
        assert "MRR" in merged

    def test_build_entity_registry(self):
        from services.corporate_memory.entities import build_entity_registry

        registry = build_entity_registry(
            groups={"engineering": {}, "finance": {}},
            entity_config={"metrics": ["churn", "MRR"]},
            metric_names=["revenue"],
        )
        assert "teams" in registry
        assert "engineering" in registry["teams"]
        assert "metrics" in registry
        assert "churn" in registry["metrics"]


class TestSessionParsing:
    """Test JSONL session file parsing (no LLM)."""

    def test_parse_correction_session(self):
        from services.session_pipeline.lib import parse_jsonl as parse_session

        turns = parse_session(SESSIONS_DIR / "correction_churn_metric.jsonl")
        assert len(turns) == 4
        assert turns[0]["role"] == "assistant"
        assert turns[1]["role"] == "user"
        assert "wrong" in turns[1]["content"].lower()

    def test_parse_empty_file(self, tmp_path):
        from services.session_pipeline.lib import parse_jsonl as parse_session

        empty_file = tmp_path / "empty.jsonl"
        empty_file.write_text("")
        turns = parse_session(empty_file)
        assert turns == []

    def test_parse_malformed_line_skipped(self, tmp_path):
        from services.session_pipeline.lib import parse_jsonl as parse_session

        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text('{"role": "user", "content": "ok"}\nNOT_JSON\n{"role": "assistant", "content": "sure"}\n')
        turns = parse_session(bad_file)
        assert len(turns) == 2  # malformed line skipped


class TestVerificationIdGeneration:
    """Test deterministic ID generation."""

    def test_deterministic(self):
        from services.verification_detector.detector import _generate_id

        id1 = _generate_id("Churn metric", "MRR based")
        id2 = _generate_id("Churn metric", "MRR based")
        assert id1 == id2
        assert id1.startswith("kv_")

    def test_different_content_different_id(self):
        from services.verification_detector.detector import _generate_id

        id1 = _generate_id("Churn metric", "MRR based")
        id2 = _generate_id("Churn metric", "Customer based")
        assert id1 != id2


class TestSchemaValidation:
    """Validate golden files against VERIFICATION_SCHEMA without LLM."""

    def test_correction_golden_valid(self):
        import jsonschema
        from services.verification_detector.schemas import VERIFICATION_SCHEMA

        golden = _load_golden("correction_churn_metric")
        jsonschema.validate(golden, VERIFICATION_SCHEMA)

    def test_empty_golden_valid(self):
        import jsonschema
        from services.verification_detector.schemas import VERIFICATION_SCHEMA

        golden = _load_golden("no_verifications")
        jsonschema.validate(golden, VERIFICATION_SCHEMA)

    def test_mixed_golden_valid(self):
        import jsonschema
        from services.verification_detector.schemas import VERIFICATION_SCHEMA

        golden = _load_golden("mixed_session")
        jsonschema.validate(golden, VERIFICATION_SCHEMA)


# ===========================================================================
# TIER 2: Integration Tests (mocked LLM)
# ===========================================================================


class TestVerificationDetectorIntegration:
    """Full pipeline tests with mocked LLM extractor."""

    def test_correction_pipeline(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        run = _run_verification_processor

        golden = _load_golden("correction_churn_metric")
        extractor = _mock_extractor(golden)

        # Setup session data
        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        import shutil

        shutil.copy(SESSIONS_DIR / "correction_churn_metric.jsonl", session_dir / "s1.jsonl")

        stats = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        assert stats["sessions_processed"] == 1
        assert stats["verifications_extracted"] == 1
        assert stats["items_created"] == 1

        repo = KnowledgeRepository(conn)
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        assert items[0]["source_type"] == "user_verification"
        assert items[0]["domain"] == "finance"
        assert items[0]["confidence"] == 0.90
        conn.close()

    def test_empty_session_skipped(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        run = _run_verification_processor

        golden = _load_golden("no_verifications")
        extractor = _mock_extractor(golden)

        session_dir = tmp_path / "user_sessions" / "bob"
        session_dir.mkdir(parents=True)
        import shutil

        shutil.copy(SESSIONS_DIR / "no_verifications.jsonl", session_dir / "s1.jsonl")

        stats = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        assert stats["sessions_processed"] == 1
        assert stats["verifications_extracted"] == 0
        assert stats["items_created"] == 0
        conn.close()

    def test_idempotency(self, tmp_path, monkeypatch):
        """Running twice on same session should not create duplicate items."""
        conn = _fresh_db(tmp_path, monkeypatch)
        run = _run_verification_processor

        golden = _load_golden("correction_churn_metric")
        extractor = _mock_extractor(golden)

        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        import shutil

        shutil.copy(SESSIONS_DIR / "correction_churn_metric.jsonl", session_dir / "s1.jsonl")

        # Run twice
        stats1 = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")
        stats2 = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        assert stats1["items_created"] == 1
        # Post-refactor: stable sessions (mtime <= processed_at) are filtered
        # at scan via the mtime precheck so the runner never sees them →
        # `scanned == 0`, not `skipped == 1`. PR #232 review fix avoided an
        # MD5-rehash storm per scheduler tick.
        assert stats2["sessions_processed"] == 0
        assert stats2["scanned"] == 0
        assert stats2["items_created"] == 0
        conn.close()

    def test_mixed_session_multiple_items(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        run = _run_verification_processor

        golden = _load_golden("mixed_session")
        extractor = _mock_extractor(golden)

        session_dir = tmp_path / "user_sessions" / "carol"
        session_dir.mkdir(parents=True)
        import shutil

        shutil.copy(SESSIONS_DIR / "mixed_session.jsonl", session_dir / "s1.jsonl")

        stats = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        assert stats["verifications_extracted"] == 2
        assert stats["items_created"] == 2

        repo = KnowledgeRepository(conn)
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 2
        conn.close()

    # The legacy `dry_run` flag was dropped in the session-pipeline refactor —
    # there is no equivalent in the new framework. The runner always persists
    # state on success; the only way to observe a "what would happen" output
    # is to wrap the processor in a transaction-rolling-back fixture, which
    # is more trouble than the test was worth (it only validated a flag that
    # had one in-tree caller — the dropped CLI shim).


class TestContradictionDetectionIntegration:
    """Batched contradiction detection (ADR Decision 4): one Haiku call returns
    judgments for every same-domain candidate, including structured
    suggested_resolution.
    """

    @staticmethod
    def _judgment(
        candidate_id, *, contradicts=False, severity=None, explanation="", action=None, merged=None, justification=None
    ):
        return {
            "candidate_id": candidate_id,
            "is_contradiction": contradicts,
            "severity": severity,
            "explanation": explanation,
            "resolution_action": action,
            "resolution_merged_content": merged,
            "resolution_justification": justification,
        }

    def test_contradiction_detected(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import check_contradictions

        repo = KnowledgeRepository(conn)
        repo.create(
            id="k_existing",
            title="Churn is customer-count based",
            content="Churn = customers lost / total customers",
            category="business_logic",
            domain="finance",
            status="approved",
        )

        new_item = {
            "id": "k_new",
            "title": "Churn is revenue-based",
            "content": "Churn = MRR lost / total MRR",
            "domain": "finance",
        }

        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "judgments": [
                self._judgment(
                    "k_existing",
                    contradicts=True,
                    severity="hard",
                    explanation="customer-count vs revenue-based",
                    action="kept_a",
                    justification="new item is more accurate",
                )
            ],
        }

        contradictions = check_contradictions(extractor, new_item, repo)
        # Content rule (vibecoding): assert exact values, not just count.
        assert len(contradictions) == 1
        assert contradictions[0]["item_a_id"] == "k_new"
        assert contradictions[0]["item_b_id"] == "k_existing"
        assert contradictions[0]["severity"] == "hard"
        assert contradictions[0]["suggested_resolution"]["action"] == "kept_a"
        # Single batched call — not one per candidate.
        assert extractor.extract_json.call_count == 1
        conn.close()

    def test_no_contradiction(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import check_contradictions

        repo = KnowledgeRepository(conn)
        repo.create(
            id="k_existing",
            title="NPS is measured quarterly",
            content="NPS survey every quarter",
            category="business_logic",
            domain="product",
            status="approved",
        )

        new_item = {
            "id": "k_new",
            "title": "NPS response rate",
            "content": "NPS has 40% response rate",
            "domain": "product",
        }

        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "judgments": [
                self._judgment(
                    "k_existing",
                    contradicts=False,
                    explanation="Different aspects of NPS",
                )
            ],
        }

        contradictions = check_contradictions(extractor, new_item, repo)
        assert contradictions == []
        # The Haiku call still happens — the *judgment* is what says no.
        assert extractor.extract_json.call_count == 1
        conn.close()

    def test_no_candidates_skips_llm(self, tmp_path, monkeypatch):
        """Cost guard: empty corpus → no Haiku call at all."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import check_contradictions

        repo = KnowledgeRepository(conn)  # no items

        new_item = {
            "id": "k_new",
            "title": "Something new",
            "content": "Brand new knowledge",
            "domain": "finance",
        }

        extractor = MagicMock()
        contradictions = check_contradictions(extractor, new_item, repo)
        assert contradictions == []
        extractor.extract_json.assert_not_called()
        conn.close()

    def test_detect_and_record_persists_structured_resolution(self, tmp_path, monkeypatch):
        """detect_and_record persists, and suggested_resolution round-trips
        as a dict (JSON-encoded in DB, decoded on read)."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import detect_and_record

        repo = KnowledgeRepository(conn)
        repo.create(
            id="k_existing",
            title="Churn is customer-count based",
            content="Churn = customers lost / total customers",
            category="business_logic",
            domain="finance",
            status="approved",
        )

        new_item = {
            "id": "k_new",
            "title": "Churn is revenue-based",
            "content": "Churn = MRR lost / total MRR",
            "domain": "finance",
        }

        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "judgments": [
                self._judgment(
                    "k_existing",
                    contradicts=True,
                    severity="hard",
                    explanation="conflicting definitions",
                    action="merge",
                    merged="Churn rolls up both views; track both metrics.",
                    justification="both useful at different reporting layers",
                )
            ],
        }

        cids = detect_and_record(extractor, new_item, repo)
        assert len(cids) == 1

        contradictions = repo.list_contradictions(resolved=False)
        assert len(contradictions) == 1
        c = contradictions[0]
        assert c["severity"] == "hard"
        # suggested_resolution round-trips as a structured dict.
        res = c["suggested_resolution"]
        assert isinstance(res, dict)
        assert res["action"] == "merge"
        assert res["merged_content"].startswith("Churn rolls up")
        assert "both useful" in res["justification"]
        conn.close()


# ===========================================================================
# Regression tests — pd-ps review (V1 must-fix)
# ===========================================================================


class TestContradictionCandidateSqlNarrowing:
    """Repository candidate narrowing (ADR Decision 4).

    Domain is the only SQL narrowing applied. Topic / content matching is
    delegated to Haiku in services.corporate_memory.contradiction.
    """

    def test_domain_excludes_other_domain(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)

        repo.create(
            id="k_finance",
            title="Churn is MRR-based",
            content="finance fact",
            category="x",
            domain="finance",
            status="approved",
        )
        repo.create(
            id="k_data", title="Churn pipeline doc", content="data fact", category="x", domain="data", status="approved"
        )

        candidates = repo.find_contradiction_candidates(
            new_item_id="k_new",
            domain="finance",
        )
        assert {c["id"] for c in candidates} == {"k_finance"}
        conn.close()

    def test_no_domain_returns_all_approved_items(self, tmp_path, monkeypatch):
        """When the new item has no domain, all approved/mandatory/pending
        items are surfaced — the LLM does the narrowing."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)
        repo.create(id="k1", title="A", content="a", category="x", status="approved")
        repo.create(id="k2", title="B", content="b", category="x", status="pending")
        repo.create(id="k3", title="C", content="c", category="x", status="rejected")

        candidates = repo.find_contradiction_candidates(new_item_id="k_new")
        ids = {c["id"] for c in candidates}
        # rejected items are out; approved + pending stay in.
        assert ids == {"k1", "k2"}
        conn.close()

    def test_domain_only_returns_all_same_domain(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)
        repo.create(id="f1", title="Revenue", content="x", category="x", domain="finance", status="approved")
        repo.create(id="f2", title="Margin", content="y", category="x", domain="finance", status="approved")
        repo.create(id="p1", title="Churn", content="z", category="x", domain="product", status="approved")
        candidates = repo.find_contradiction_candidates(new_item_id="k_new", domain="finance")
        assert {c["id"] for c in candidates} == {"f1", "f2"}
        conn.close()

    def test_self_id_excluded_from_candidates(self, tmp_path, monkeypatch):
        """An item never contradicts itself — id != new_item_id must be enforced."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)
        repo.create(id="self_id", title="Churn metric", content="x", category="x", domain="finance", status="approved")
        repo.create(id="other", title="Churn metric", content="y", category="x", domain="finance", status="approved")
        candidates = repo.find_contradiction_candidates(
            new_item_id="self_id",
            domain="finance",
        )
        assert {c["id"] for c in candidates} == {"other"}
        conn.close()

    def test_personal_items_excluded_from_contradiction_candidates(self, tmp_path, monkeypatch):
        """Personal items must NOT enter the LLM prompt as candidates — the
        Haiku call is a read site that exfiltrates content to the external
        API, and the LLM can paraphrase personal content into the persisted
        knowledge_contradictions.suggested_resolution.merged_content. ADR
        Decision 1 ("hard privacy boundary, not a UI hint") applies here."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)
        repo.create(
            id="public",
            title="Public def",
            content="x",
            category="x",
            domain="finance",
            status="approved",
            is_personal=False,
        )
        repo.create(
            id="private",
            title="Private def",
            content="confidential",
            category="x",
            domain="finance",
            status="approved",
            is_personal=True,
        )

        candidates = repo.find_contradiction_candidates(
            new_item_id="k_new",
            domain="finance",
        )
        ids = {c["id"] for c in candidates}
        assert ids == {"public"}
        # Defense in depth: also confirm by content match — even if the SQL
        # changed shape, no row carrying "confidential" must come back.
        assert all("confidential" not in (c.get("content") or "") for c in candidates)
        conn.close()


class TestDetectorIgnoresLLMConfidence:
    """Q3: LLM-supplied base_confidence in golden must be ignored.

    Confidence is derived in code from (source_type, detection_type) — never
    from the LLM output, even if a malicious or hallucinating model returns one.
    """

    def test_llm_returned_base_confidence_is_overridden(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        run = _run_verification_processor

        # Hostile golden: LLM tries to claim confidence=0.99 on a confirmation
        # (which should be 0.60 in code).
        hostile = {
            "verifications": [
                {
                    "detection_type": "confirmation",
                    "title": "Hostile claim",
                    "content": "LLM-elevated content",
                    "user_quote": "yep",
                    "domain": "engineering",
                    "entities": [],
                    "base_confidence": 0.99,
                }
            ]
        }
        extractor = _mock_extractor(hostile)

        session_dir = tmp_path / "user_sessions" / "mallory"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(json.dumps({"role": "user", "content": "yep"}) + "\n")

        run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        repo = KnowledgeRepository(conn)
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        # 0.60 is the canonical (user_verification, confirmation) value, not
        # the 0.99 the LLM tried to inject.
        assert items[0]["confidence"] == 0.60
        # Belt-and-suspenders: the LLM-supplied base_confidence must never
        # round-trip onto the persisted item. If a future code change
        # reintroduces a base_confidence read path (e.g. into a new metadata
        # JSON column), this assertion will catch it.
        assert "base_confidence" not in items[0]
        conn.close()

    def test_unknown_detection_type_falls_back_to_canonical_value(self, tmp_path, monkeypatch):
        """If the LLM hallucinates a detection_type, fall back to the canonical
        (user_verification, confirmation) baseline rather than crashing or
        accepting an LLM-supplied number."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        run = _run_verification_processor

        hallucinated = {
            "verifications": [
                {
                    "detection_type": "totally_made_up_type",
                    "title": "Hostile claim",
                    "content": "x",
                    "user_quote": "y",
                    "domain": "engineering",
                    "entities": [],
                }
            ]
        }
        extractor = _mock_extractor(hallucinated)

        session_dir = tmp_path / "user_sessions" / "mallory"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(json.dumps({"role": "user", "content": "y"}) + "\n")

        run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        repo = KnowledgeRepository(conn)
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        assert items[0]["confidence"] == 0.60  # canonical confirmation fallback
        conn.close()


class TestDetectorPersistsEvidence:
    """Q3: user_quote and detection_type must land in verification_evidence."""

    def test_evidence_row_created_per_verification(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        run = _run_verification_processor

        golden = _load_golden("correction_churn_metric")
        extractor = _mock_extractor(golden)

        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        import shutil

        shutil.copy(SESSIONS_DIR / "correction_churn_metric.jsonl", session_dir / "s1.jsonl")

        run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        repo = KnowledgeRepository(conn)
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        evidence = repo.list_evidence(items[0]["id"])
        assert len(evidence) == 1
        assert evidence[0]["detection_type"] == "correction"
        assert evidence[0]["source_user"] == "alice"
        # source_ref pins the evidence back to the originating session.
        assert evidence[0]["source_ref"] is not None
        assert "alice" in (evidence[0]["source_ref"] or "")
        # The LLM extracts the exact quote — that signal must persist.
        assert "MRR" in (evidence[0]["user_quote"] or "")
        conn.close()

    def test_mixed_session_creates_one_evidence_row_per_verification(self, tmp_path, monkeypatch):
        """Two verifications in one session → two distinct evidence rows on
        their respective items. Each row carries its own user_quote."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        run = _run_verification_processor

        golden = _load_golden("mixed_session")
        extractor = _mock_extractor(golden)

        session_dir = tmp_path / "user_sessions" / "carol"
        session_dir.mkdir(parents=True)
        import shutil

        shutil.copy(SESSIONS_DIR / "mixed_session.jsonl", session_dir / "s.jsonl")

        run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        repo = KnowledgeRepository(conn)
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 2
        # Each item gets exactly one evidence row, with the matching user_quote.
        all_quotes = []
        for item in items:
            evs = repo.list_evidence(item["id"])
            assert len(evs) == 1
            all_quotes.append(evs[0]["user_quote"])
        # Distinct quotes — confirms we are not stamping the same user_quote on
        # both items.
        assert len(set(all_quotes)) == 2
        conn.close()

    def test_duplicate_item_id_still_records_evidence(self, tmp_path, monkeypatch):
        """When two analysts independently produce the same (title, content),
        _generate_id collides and the second run hits the dedup `continue`
        path. ADR Decision 3 requires evidence to still accumulate so the
        second analyst's user_quote / detection_type / source_user are not
        silently dropped — that's what enables the "additional verifiers"
        boost mentioned in the ADR.
        """
        import shutil

        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        run = _run_verification_processor
        repo = KnowledgeRepository(conn)
        golden = _load_golden("correction_churn_metric")

        # Session 1 — alice. Creates the item + evidence row #1.
        alice_dir = tmp_path / "user_sessions" / "alice"
        alice_dir.mkdir(parents=True)
        shutil.copy(SESSIONS_DIR / "correction_churn_metric.jsonl", alice_dir / "s.jsonl")
        run(conn, _mock_extractor(golden), session_data_dir=tmp_path / "user_sessions")

        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        item_id = items[0]["id"]
        evidence_after_alice = repo.list_evidence(item_id)
        assert len(evidence_after_alice) == 1
        assert evidence_after_alice[0]["source_user"] == "alice"

        # Session 2 — bob. Same golden output (same title+content → same
        # _generate_id), different session/user. Item already exists, but a
        # fresh evidence row must be persisted on the existing item.
        bob_dir = tmp_path / "user_sessions" / "bob"
        bob_dir.mkdir(parents=True)
        shutil.copy(SESSIONS_DIR / "correction_churn_metric.jsonl", bob_dir / "s.jsonl")
        run(conn, _mock_extractor(golden), session_data_dir=tmp_path / "user_sessions")

        # Item count unchanged.
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        assert items[0]["id"] == item_id

        # Evidence count grew — bob's evidence accumulated on the existing item.
        evidence_after_bob = repo.list_evidence(item_id)
        assert len(evidence_after_bob) == 2
        users = {e["source_user"] for e in evidence_after_bob}
        assert users == {"alice", "bob"}
        conn.close()


class TestVerificationFuzzyDedupeGate:
    """A paraphrased re-statement of an already-known fact must not create a
    second near-duplicate PENDING item. The exact-hash check in
    _generate_id() only catches verbatim restatements — the fuzzy dedup gate
    (services/verification_detector/duplicates.py::find_duplicate_target)
    catches paraphrases via entity-tag overlap or lexical similarity, and
    merges into the existing item instead (records evidence there)."""

    def test_entity_overlap_paraphrase_merges_into_existing_item(self, tmp_path, monkeypatch):
        import shutil

        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        run = _run_verification_processor
        repo = KnowledgeRepository(conn)

        # Session 1 — establishes the canonical item.
        alice_dir = tmp_path / "user_sessions" / "alice"
        alice_dir.mkdir(parents=True)
        shutil.copy(SESSIONS_DIR / "churn_forecast_v1.jsonl", alice_dir / "s.jsonl")
        run(conn, _mock_extractor(_load_golden("churn_forecast_v1")), session_data_dir=tmp_path / "user_sessions")

        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        item_id = items[0]["id"]

        # Session 2 — bob restates the same fact in different words. The
        # title/content hash differs, but entities overlap >= 2
        # ("churn", "forecast"), so this must merge into the existing item
        # rather than create a second PENDING row.
        bob_dir = tmp_path / "user_sessions" / "bob"
        bob_dir.mkdir(parents=True)
        shutil.copy(SESSIONS_DIR / "churn_forecast_entity_paraphrase.jsonl", bob_dir / "s.jsonl")
        stats2 = run(
            conn,
            _mock_extractor(_load_golden("churn_forecast_entity_paraphrase")),
            session_data_dir=tmp_path / "user_sessions",
        )

        assert stats2["items_created"] == 0
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        assert items[0]["id"] == item_id

        evidence = repo.list_evidence(item_id)
        assert len(evidence) == 2
        assert {e["source_user"] for e in evidence} == {"alice", "bob"}
        conn.close()

    def test_churn_crm_paraphrase_merges_into_existing_item(self, tmp_path, monkeypatch):
        """Real-world-shaped regression case: two same-domain verifications
        that both restate "track forecasted churn on orders, recording why a
        client may leave, for internal reporting" with overlapping entities
        and genuinely similar wording. This is the case the gate exists to
        catch, so it must still merge under the strong-evidence rule
        (entity overlap >= MIN_ENTITY_OVERLAP together with lexical
        similarity clearing LEXICAL_MERGE_WITH_ENTITIES_THRESHOLD)."""
        import shutil

        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        run = _run_verification_processor
        repo = KnowledgeRepository(conn)

        alice_dir = tmp_path / "user_sessions" / "alice"
        alice_dir.mkdir(parents=True)
        shutil.copy(SESSIONS_DIR / "churn_crm_tracking_v1.jsonl", alice_dir / "s.jsonl")
        run(conn, _mock_extractor(_load_golden("churn_crm_tracking_v1")), session_data_dir=tmp_path / "user_sessions")

        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        item_id = items[0]["id"]

        bob_dir = tmp_path / "user_sessions" / "bob"
        bob_dir.mkdir(parents=True)
        shutil.copy(SESSIONS_DIR / "churn_crm_tracking_paraphrase.jsonl", bob_dir / "s.jsonl")
        stats2 = run(
            conn,
            _mock_extractor(_load_golden("churn_crm_tracking_paraphrase")),
            session_data_dir=tmp_path / "user_sessions",
        )

        assert stats2["items_created"] == 0
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        assert items[0]["id"] == item_id

        evidence = repo.list_evidence(item_id)
        assert len(evidence) == 2
        assert {e["source_user"] for e in evidence} == {"alice", "bob"}
        conn.close()

    def test_lexical_similarity_paraphrase_merges_into_existing_item(self, tmp_path, monkeypatch):
        """Same scenario, but the paraphrase carries too few overlapping
        entity tags for the Jaccard check to fire (only one shared/low-signal
        tag) — the lexical-similarity fallback must still catch it because
        the wording is a near-verbatim rewording."""
        import shutil

        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        run = _run_verification_processor
        repo = KnowledgeRepository(conn)

        alice_dir = tmp_path / "user_sessions" / "alice"
        alice_dir.mkdir(parents=True)
        shutil.copy(SESSIONS_DIR / "churn_forecast_v1.jsonl", alice_dir / "s.jsonl")
        run(conn, _mock_extractor(_load_golden("churn_forecast_v1")), session_data_dir=tmp_path / "user_sessions")

        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        item_id = items[0]["id"]

        bob_dir = tmp_path / "user_sessions" / "bob"
        bob_dir.mkdir(parents=True)
        shutil.copy(SESSIONS_DIR / "churn_forecast_lexical_paraphrase.jsonl", bob_dir / "s.jsonl")
        stats2 = run(
            conn,
            _mock_extractor(_load_golden("churn_forecast_lexical_paraphrase")),
            session_data_dir=tmp_path / "user_sessions",
        )

        assert stats2["items_created"] == 0
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        assert items[0]["id"] == item_id

        evidence = repo.list_evidence(item_id)
        assert len(evidence) == 2
        assert {e["source_user"] for e in evidence} == {"alice", "bob"}
        conn.close()

    def test_genuinely_different_fact_same_domain_still_creates_new_row(self, tmp_path, monkeypatch):
        """Negative case — a different fact in the same domain, with no
        entity overlap and low lexical similarity, must NOT be merged."""
        import shutil

        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        run = _run_verification_processor
        repo = KnowledgeRepository(conn)

        alice_dir = tmp_path / "user_sessions" / "alice"
        alice_dir.mkdir(parents=True)
        shutil.copy(SESSIONS_DIR / "churn_forecast_v1.jsonl", alice_dir / "s.jsonl")
        run(conn, _mock_extractor(_load_golden("churn_forecast_v1")), session_data_dir=tmp_path / "user_sessions")

        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        first_id = items[0]["id"]

        # unprompted_definition.json is also domain="finance" but is an
        # unrelated fact (CAC definition) — must land as its own item.
        bob_dir = tmp_path / "user_sessions" / "bob"
        bob_dir.mkdir(parents=True)
        shutil.copy(SESSIONS_DIR / "unprompted_definition.jsonl", bob_dir / "s.jsonl")
        stats2 = run(
            conn,
            _mock_extractor(_load_golden("unprompted_definition")),
            session_data_dir=tmp_path / "user_sessions",
        )

        assert stats2["items_created"] == 1
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 2
        ids = {item["id"] for item in items}
        assert first_id in ids
        conn.close()

    def test_contradicting_correction_is_not_fuzzy_merged(self, tmp_path, monkeypatch):
        """A ``correction`` may *overturn* a stored fact, not merely restate
        it. Even when it is a near-verbatim reword of an existing item (high
        lexical similarity + entity overlap — signals that would otherwise
        trip the fuzzy-merge gate), it must NOT be absorbed as confirming
        evidence: that would both discard the corrected content and skip the
        contradiction check that only runs on the create path. Corrections
        are routed to create instead, so ``detect_and_record`` can fire."""
        import shutil

        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        run = _run_verification_processor
        repo = KnowledgeRepository(conn)

        # Session 1 — establishes "churn is computed monthly".
        alice_dir = tmp_path / "user_sessions" / "alice"
        alice_dir.mkdir(parents=True)
        shutil.copy(SESSIONS_DIR / "churn_forecast_v1.jsonl", alice_dir / "s.jsonl")
        run(conn, _mock_extractor(_load_golden("churn_forecast_v1")), session_data_dir=tmp_path / "user_sessions")

        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        first_id = items[0]["id"]

        # Session 2 — bob corrects it to "weekly". Near-verbatim wording
        # (lexical ratio ~0.93) and 2 shared entities would clear the
        # fuzzy-merge gate, but because it is a correction it must land as
        # its own item rather than merging into the "monthly" one.
        # detect_and_record's LLM judge is stubbed — this test pins the
        # routing decision, not the judge's output (covered separately).
        import services.corporate_memory.contradiction as contradiction_module

        monkeypatch.setattr(contradiction_module, "detect_and_record", lambda *a, **k: [])

        bob_dir = tmp_path / "user_sessions" / "bob"
        bob_dir.mkdir(parents=True)
        shutil.copy(SESSIONS_DIR / "churn_forecast_contradicting_correction.jsonl", bob_dir / "s.jsonl")
        stats2 = run(
            conn,
            _mock_extractor(_load_golden("churn_forecast_contradicting_correction")),
            session_data_dir=tmp_path / "user_sessions",
        )

        assert stats2["items_created"] == 1
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 2
        assert first_id in {item["id"] for item in items}
        conn.close()


class TestDetectorWiresContradictionDetection:
    """Q2: detect_and_record() must run after repo.create() in the pipeline."""

    def test_contradiction_recorded_when_judge_says_yes(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        run = _run_verification_processor
        from unittest.mock import MagicMock

        repo = KnowledgeRepository(conn)
        # Pre-existing approved item that the new one will conflict with.
        repo.create(
            id="existing",
            title="Churn definition",
            content="Customer-count based",
            category="business_logic",
            domain="finance",
            status="approved",
        )

        # Stub extractor: first call returns a verification; subsequent calls
        # (the contradiction judge) return contradicts=True.
        verification_response = {
            "verifications": [
                {
                    "detection_type": "correction",
                    "title": "Churn is MRR-based",
                    "content": "Revenue-based",
                    "user_quote": "MRR-based, not customer-count",
                    "domain": "finance",
                    "entities": ["churn", "MRR"],
                }
            ]
        }
        contradiction_response = {
            "judgments": [
                {
                    "candidate_id": "existing",
                    "is_contradiction": True,
                    "severity": "hard",
                    "explanation": "definitions disagree",
                    "resolution_action": "kept_a",
                    "resolution_merged_content": None,
                    "resolution_justification": "new item is more accurate",
                }
            ]
        }

        extractor = MagicMock()
        extractor.extract_json.side_effect = [verification_response, contradiction_response]

        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(
            json.dumps({"role": "user", "content": "MRR-based, not customer-count"}) + "\n"
        )

        stats = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        assert stats["items_created"] == 1
        assert stats["contradictions_recorded"] == 1
        contradictions = repo.list_contradictions(resolved=False)
        assert len(contradictions) == 1
        assert contradictions[0]["item_b_id"] == "existing"
        conn.close()

    def test_no_contradiction_when_judge_says_no(self, tmp_path, monkeypatch):
        """Judge returns contradicts=false → item still created, contradictions_recorded=0."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        run = _run_verification_processor
        from unittest.mock import MagicMock

        repo = KnowledgeRepository(conn)
        repo.create(
            id="existing",
            title="Churn definition",
            content="Customer-count based",
            category="business_logic",
            domain="finance",
            status="approved",
        )

        extractor = MagicMock()
        extractor.extract_json.side_effect = [
            {
                "verifications": [
                    {
                        "detection_type": "correction",
                        "title": "Churn refinement",
                        "content": "Same as existing, more detail",
                        "user_quote": "more detail",
                        "domain": "finance",
                        "entities": ["churn"],
                    }
                ]
            },
            {
                "judgments": [
                    {
                        "candidate_id": "existing",
                        "is_contradiction": False,
                        "severity": None,
                        "explanation": "compatible — different scopes",
                        "resolution_action": None,
                        "resolution_merged_content": None,
                        "resolution_justification": None,
                    }
                ]
            },
        ]

        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(json.dumps({"role": "user", "content": "more detail"}) + "\n")

        stats = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")
        assert stats["items_created"] == 1
        assert stats["contradictions_recorded"] == 0
        assert repo.list_contradictions(resolved=False) == []
        conn.close()

    def test_contradiction_judge_failure_does_not_abort_run(self, tmp_path, monkeypatch):
        """If the contradiction judge raises LLMError, the item must still be
        created and the session must still be marked processed. Failure of the
        judge is degraded mode, not a fatal error."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        run = _run_verification_processor
        from connectors.llm.exceptions import LLMError
        from unittest.mock import MagicMock

        repo = KnowledgeRepository(conn)
        repo.create(
            id="existing",
            title="Churn definition",
            content="x",
            category="business_logic",
            domain="finance",
            status="approved",
        )

        verification_response = {
            "verifications": [
                {
                    "detection_type": "correction",
                    "title": "Churn override",
                    "content": "y",
                    "user_quote": "z",
                    "domain": "finance",
                    "entities": ["churn"],
                }
            ]
        }
        extractor = MagicMock()
        extractor.extract_json.side_effect = [
            verification_response,
            LLMError("simulated judge failure"),
        ]

        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(json.dumps({"role": "user", "content": "z"}) + "\n")

        stats = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")
        # Item lands; judge failure is logged and swallowed.
        assert stats["items_created"] == 1
        assert stats["contradictions_recorded"] == 0
        assert stats["sessions_processed"] == 1
        # Session is marked processed so we don't re-run on next sweep.
        from services.session_pipeline.lib import compute_file_hash
        from src.repositories.session_processor_state import SessionProcessorStateRepository

        state_repo = SessionProcessorStateRepository(conn)
        h = compute_file_hash(session_dir / "s.jsonl")
        assert state_repo.is_processed("verification", "alice/s.jsonl", h) is True
        conn.close()


class TestVerificationProcessorTimeBudget:
    """Prod incident 2026-07-15: a single process_session() call looped over
    dozens of verification items, each doing an inline LLM contradiction
    check, for over an hour — starving the FastAPI threadpool and causing
    app-wide 503s. process_session() must bound its own wall-clock time and
    hand remaining items back to the next scheduler tick rather than run
    unbounded."""

    def test_stops_early_and_leaves_session_unprocessed(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.session_pipeline.lib import compute_file_hash
        from src.repositories.session_processor_state import SessionProcessorStateRepository
        import services.session_processors.verification as verification_module

        repo = KnowledgeRepository(conn)

        # Three verifications in one session. Each creates a distinct item and
        # (since no pre-existing same-domain items exist) skips the
        # contradiction LLM call — find_and_judge() short-circuits on an
        # empty candidate list, so extract_verifications is the only LLM call
        # we need to stub.
        verification_response = {
            "verifications": [
                {
                    "detection_type": "correction",
                    "title": f"Fact {i}",
                    "content": f"Content {i}",
                    "user_quote": f"quote {i}",
                    "domain": "finance",
                    "entities": [f"entity{i}"],
                }
                for i in range(3)
            ]
        }
        extractor = MagicMock()
        extractor.extract_json.return_value = verification_response

        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        session_path = session_dir / "s.jsonl"
        session_path.write_text(json.dumps({"role": "user", "content": "quote 0"}) + "\n")

        # monotonic() sequence: [start, check-item0 (still under budget),
        # check-item1 (budget blown)] — item0 gets fully processed, item1/2
        # never start.
        monkeypatch.setattr(
            verification_module.time,
            "monotonic",
            MagicMock(side_effect=[0.0, 0.0, verification_module._TIME_BUDGET_SECONDS + 1]),
        )

        processor = verification_module.VerificationProcessor(extractor)
        with pytest.raises(verification_module.TimeBudgetExceeded):
            processor.process_session(session_path, "alice", "alice/s.jsonl", conn)

        # Only the first item was persisted — proof the loop actually stopped
        # rather than raising before doing any work.
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        assert items[0]["title"] == "Fact 0"

        # The runner must see this as "not processed" so the session stays
        # eligible for pickup on the next scheduler tick — no state row.
        state_repo = SessionProcessorStateRepository(conn)
        h = compute_file_hash(session_path)
        assert state_repo.is_processed("verification", "alice/s.jsonl", h) is False
        conn.close()

    def test_runner_retries_session_after_budget_exceeded(self, tmp_path, monkeypatch):
        """End-to-end through run_processor(): a budget-exceeded session is
        counted as an error this tick (not silently dropped) and remains a
        scan candidate on the next tick."""
        conn = _fresh_db(tmp_path, monkeypatch)
        import services.session_processors.verification as verification_module
        from services.session_pipeline.runner import run_processor

        verification_response = {
            "verifications": [
                {
                    "detection_type": "correction",
                    "title": f"Fact {i}",
                    "content": f"Content {i}",
                    "user_quote": f"quote {i}",
                    "domain": "finance",
                    "entities": [f"entity{i}"],
                }
                for i in range(2)
            ]
        }
        extractor = MagicMock()
        extractor.extract_json.return_value = verification_response

        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(json.dumps({"role": "user", "content": "quote 0"}) + "\n")

        monkeypatch.setattr(
            verification_module.time,
            "monotonic",
            MagicMock(side_effect=[0.0, 0.0, verification_module._TIME_BUDGET_SECONDS + 1]),
        )

        processor = verification_module.VerificationProcessor(extractor)
        stats = run_processor(conn, processor, session_data_dir=tmp_path / "user_sessions")

        assert stats["scanned"] == 1
        assert stats["processed"] == 0
        assert stats["errors"] == 1
        conn.close()

    def test_retry_does_not_duplicate_evidence_for_already_processed_items(self, tmp_path, monkeypatch):
        """A session retried after TimeBudgetExceeded re-extracts the same
        verifications on the next tick. Items already created on the first
        tick must not get a second evidence row appended for the same
        (source_user, source_ref) — that would silently inflate the
        confirmation signal every retry."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        import services.session_processors.verification as verification_module
        from services.verification_detector.detector import _generate_id

        repo = KnowledgeRepository(conn)

        verification_response = {
            "verifications": [
                {
                    "detection_type": "correction",
                    "title": "Fact 0",
                    "content": "Content 0",
                    "user_quote": "quote 0",
                    "domain": "finance",
                    "entities": ["entity0"],
                },
                {
                    "detection_type": "correction",
                    "title": "Fact 1",
                    "content": "Content 1",
                    "user_quote": "quote 1",
                    "domain": "finance",
                    "entities": ["entity1"],
                },
            ]
        }
        extractor = MagicMock()
        extractor.extract_json.return_value = verification_response

        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        session_path = session_dir / "s.jsonl"
        session_path.write_text(json.dumps({"role": "user", "content": "quote 0"}) + "\n")

        processor = verification_module.VerificationProcessor(extractor)

        # First tick: budget blows after item 0 is fully created — item 1
        # never starts.
        monkeypatch.setattr(
            verification_module.time,
            "monotonic",
            MagicMock(side_effect=[0.0, 0.0, verification_module._TIME_BUDGET_SECONDS + 1]),
        )
        with pytest.raises(verification_module.TimeBudgetExceeded):
            processor.process_session(session_path, "alice", "alice/s.jsonl", conn)

        item0_id = _generate_id("Fact 0", "Content 0")
        assert len(repo.list_evidence(item0_id)) == 1

        # Second tick (retry): budget doesn't blow this time, both items are
        # re-extracted. Item 0 hash-collides (already exists) — it must not
        # get a second evidence row. Item 1 is genuinely new.
        monkeypatch.setattr(verification_module.time, "monotonic", MagicMock(return_value=0.0))
        result = processor.process_session(session_path, "alice", "alice/s.jsonl", conn)

        assert len(repo.list_evidence(item0_id)) == 1
        item1_id = _generate_id("Fact 1", "Content 1")
        assert len(repo.list_evidence(item1_id)) == 1
        assert result.items_count == 1
        conn.close()


class TestBatchContradictionSchemaStrictValid:
    """Guard BATCH_CONTRADICTION_SCHEMA against the enum+union-type schema rejection regression.

    The bug: ``severity`` / ``resolution_action`` declared a union type
    ``["string","null"]`` AND an ``enum`` containing ``None``. Strict
    structured outputs reject that combination -> every contradiction check
    400s. The fix keeps the enum (so the model can't emit out-of-range values)
    but splits each nullable field into ``anyOf`` of a string-with-enum branch
    and a null branch.
    """

    @staticmethod
    def _walk(node):
        """Yield every dict node in a JSON-schema tree."""
        if isinstance(node, dict):
            yield node
            for v in node.values():
                yield from TestBatchContradictionSchemaStrictValid._walk(v)
        elif isinstance(node, list):
            for item in node:
                yield from TestBatchContradictionSchemaStrictValid._walk(item)

    def test_no_node_combines_enum_with_union_type_or_null_enum(self):
        """Anti-regression for the enum+union-type schema rejection pattern.

        Walk the schema as it is actually sent (after _strict_json_schema) and
        assert no node combines an ``enum`` with a list-typed ``type``, and no
        ``enum`` contains ``None``. Protects future schemas too.
        """
        from connectors.llm.anthropic_provider import _strict_json_schema
        from services.corporate_memory.prompts import BATCH_CONTRADICTION_SCHEMA

        strict = _strict_json_schema(BATCH_CONTRADICTION_SCHEMA)
        for node in self._walk(strict):
            if "enum" not in node:
                continue
            assert not isinstance(node.get("type"), list), (
                f"enum combined with a union type is rejected by strict outputs: {node!r}"
            )
            assert None not in node["enum"], f"enum containing None is rejected by strict outputs: {node!r}"

    def test_nullable_enum_fields_preserve_strict_enum_via_anyof(self):
        """The fix must not loosen the constraint -- enum values stay enforced.

        ``severity`` / ``resolution_action`` must be ``anyOf`` of a
        string-with-enum branch (matching the code's valid sets) plus a null
        branch, so the model still cannot emit out-of-range values.
        """
        from services.corporate_memory.contradiction import (
            _VALID_ACTIONS,
            _VALID_SEVERITIES,
        )
        from services.corporate_memory.prompts import BATCH_CONTRADICTION_SCHEMA

        item_props = BATCH_CONTRADICTION_SCHEMA["properties"]["judgments"]["items"]["properties"]
        for field, valid in (
            ("severity", _VALID_SEVERITIES),
            ("resolution_action", _VALID_ACTIONS),
        ):
            spec = item_props[field]
            assert "anyOf" in spec, f"{field} must use anyOf, got {spec!r}"
            branches = spec["anyOf"]
            assert {"type": "null"} in branches, f"{field} must allow null: {spec!r}"
            enum_branches = [b for b in branches if "enum" in b]
            assert len(enum_branches) == 1, f"{field} needs one enum branch: {spec!r}"
            assert enum_branches[0]["type"] == "string"
            assert set(enum_branches[0]["enum"]) == valid, (
                f"{field} enum drifted from the code's valid set {valid}: {spec!r}"
            )


class TestBatchedContradictionFindAndJudge:
    """Direct unit tests for find_and_judge — the new batched Haiku path
    (ADR Decision 4). Covers hallucination-defense, severity normalization,
    structured resolution shape, and the single-call cost guarantee.
    """

    @staticmethod
    def _judgment(
        candidate_id, *, contradicts=False, severity=None, explanation="", action=None, merged=None, justification=None
    ):
        return {
            "candidate_id": candidate_id,
            "is_contradiction": contradicts,
            "severity": severity,
            "explanation": explanation,
            "resolution_action": action,
            "resolution_merged_content": merged,
            "resolution_justification": justification,
        }

    def _seed(self, repo, n: int, domain: str = "finance"):
        ids = []
        for i in range(n):
            cid = f"c{i}"
            repo.create(
                id=cid,
                title=f"Item {i}",
                content=f"content {i}",
                category="business_logic",
                domain=domain,
                status="approved",
            )
            ids.append(cid)
        return ids

    def test_single_batched_call_for_many_candidates(self, tmp_path, monkeypatch):
        """Cost-shape guarantee: one extract_json call regardless of N
        candidates. Replaces the old N-call sequential pattern."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import find_and_judge

        repo = KnowledgeRepository(conn)
        ids = self._seed(repo, 5)

        new_item = {"id": "k_new", "title": "New", "content": "x", "domain": "finance"}
        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "judgments": [self._judgment(cid, contradicts=False) for cid in ids],
        }

        records = find_and_judge(extractor, new_item, repo)
        assert records == []
        # Single call regardless of corpus size — this is the whole point.
        assert extractor.extract_json.call_count == 1
        conn.close()

    def test_hallucinated_candidate_id_dropped(self, tmp_path, monkeypatch):
        """If Haiku returns a candidate_id that wasn't in the input list, drop
        it. Defends against schema-conformant but fabricated IDs."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import find_and_judge

        repo = KnowledgeRepository(conn)
        self._seed(repo, 1)  # only c0 exists

        new_item = {"id": "k_new", "title": "New", "content": "x", "domain": "finance"}
        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "judgments": [
                self._judgment(
                    "c0",
                    contradicts=True,
                    severity="hard",
                    explanation="real",
                    action="kept_a",
                    justification="new wins",
                ),
                self._judgment(
                    "hallucinated_id_that_does_not_exist",
                    contradicts=True,
                    severity="hard",
                    explanation="fake",
                    action="kept_a",
                    justification="should be dropped",
                ),
            ],
        }

        records = find_and_judge(extractor, new_item, repo)
        assert len(records) == 1
        assert records[0]["item_b_id"] == "c0"
        conn.close()

    def test_mixed_batch_only_persists_contradictions(self, tmp_path, monkeypatch):
        """Three candidates, only one contradicts — only that one is recorded.
        Critical to confirm we don't store every judgment, only positives."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import detect_and_record

        repo = KnowledgeRepository(conn)
        ids = self._seed(repo, 3)

        new_item = {"id": "k_new", "title": "New", "content": "x", "domain": "finance"}
        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "judgments": [
                self._judgment(ids[0], contradicts=False, explanation="not related"),
                self._judgment(
                    ids[1],
                    contradicts=True,
                    severity="soft",
                    explanation="possibly outdated",
                    action="kept_a",
                    justification="new is more recent",
                ),
                self._judgment(ids[2], contradicts=False, explanation="orthogonal"),
            ],
        }

        cids = detect_and_record(extractor, new_item, repo)
        assert len(cids) == 1
        contradictions = repo.list_contradictions(resolved=False)
        assert len(contradictions) == 1
        assert contradictions[0]["item_b_id"] == ids[1]
        assert contradictions[0]["severity"] == "soft"
        conn.close()

    def test_invalid_severity_normalized_to_none(self, tmp_path, monkeypatch):
        """A severity value outside {'hard', 'soft'} is normalized to None.
        Schema enum should already block this, but defense-in-depth."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import find_and_judge

        repo = KnowledgeRepository(conn)
        self._seed(repo, 1)

        new_item = {"id": "k_new", "title": "New", "content": "x", "domain": "finance"}
        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "judgments": [
                self._judgment(
                    "c0",
                    contradicts=True,
                    severity="catastrophic",  # not in enum
                    explanation="severe but unparseable",
                    action="kept_a",
                    justification="new wins",
                )
            ],
        }

        records = find_and_judge(extractor, new_item, repo)
        assert len(records) == 1
        assert records[0]["severity"] is None

    def test_invalid_resolution_action_dropped(self, tmp_path, monkeypatch):
        """Unknown action is dropped from the persisted record (record stays,
        suggested_resolution is omitted)."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import find_and_judge

        repo = KnowledgeRepository(conn)
        self._seed(repo, 1)

        new_item = {"id": "k_new", "title": "New", "content": "x", "domain": "finance"}
        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "judgments": [
                self._judgment(
                    "c0",
                    contradicts=True,
                    severity="hard",
                    explanation="x",
                    action="rewrite_from_scratch",  # not in enum
                    justification="y",
                )
            ],
        }

        records = find_and_judge(extractor, new_item, repo)
        assert len(records) == 1
        # Bad action means no resolution stored; the contradiction itself stays.
        assert "suggested_resolution" not in records[0]

    def test_merge_action_carries_merged_content(self, tmp_path, monkeypatch):
        """When action is 'merge', merged_content must persist on the
        suggested_resolution dict."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import detect_and_record

        repo = KnowledgeRepository(conn)
        self._seed(repo, 1)

        new_item = {"id": "k_new", "title": "New", "content": "x", "domain": "finance"}
        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "judgments": [
                self._judgment(
                    "c0",
                    contradicts=True,
                    severity="soft",
                    explanation="overlap",
                    action="merge",
                    merged="Both definitions co-exist; track separately and reconcile quarterly.",
                    justification="non-conflicting scopes",
                )
            ],
        }

        detect_and_record(extractor, new_item, repo)
        c = repo.list_contradictions(resolved=False)[0]
        res = c["suggested_resolution"]
        assert res["action"] == "merge"
        assert "co-exist" in res["merged_content"]
        conn.close()

    def test_legacy_string_resolution_still_readable(self, tmp_path, monkeypatch):
        """Backwards compat: rows persisted before ADR D4 carry plain-string
        suggested_resolution. Must continue to be readable as a string."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)
        repo.create(id="a", title="x", content="x", category="x", status="approved")
        repo.create(id="b", title="y", content="y", category="x", status="approved")
        repo.create_contradiction(
            item_a_id="a",
            item_b_id="b",
            explanation="legacy",
            severity="hard",
            suggested_resolution="kept_a — see notes",  # plain string
        )
        c = repo.list_contradictions(resolved=False)[0]
        # Plain string is preserved as-is (not coerced into a dict).
        assert c["suggested_resolution"] == "kept_a — see notes"
        conn.close()


class TestExponentialDecayWithLinearFallback:
    """Verify exponential decay and linear fallback via configure()."""

    def test_linear_mode_still_works(self):
        import copy
        from services.corporate_memory import confidence as cm

        orig = copy.deepcopy(cm._DECAY_CONFIG)
        try:
            cm.configure({"decay": {"mode": "linear", "decay_rate_monthly": 0.02, "floor": {"default": 0.0}}})
            created = datetime.now(timezone.utc) - timedelta(days=60)
            c = cm.apply_decay(0.90, created)
            assert c < 0.90
            assert c == pytest.approx(0.86, abs=0.01)
        finally:
            cm._DECAY_CONFIG.clear()
            cm._DECAY_CONFIG.update(orig)


def _write_corporate_memory_config(tmp_path, cm_config: dict) -> None:
    """Write a `corporate_memory` overlay to DATA_DIR/state/instance.yaml and
    drop the in-process instance.yaml cache so the next
    ``get_corporate_memory_config()`` call sees it. Mirrors
    ``tests/test_admin_server_config_corp_memory.py``'s own setup."""
    import yaml as _yaml

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instance.yaml").write_text(_yaml.dump({"corporate_memory": cm_config}))

    import app.instance_config as ic

    ic._instance_config = None


class TestSessionTranscriptsKillSwitches:
    """#1957 interim hotfix: corporate_memory.sources.session_transcripts.
    {enabled,detection_types} were documented in the schema and
    config/instance.yaml.example but VerificationProcessor.process_session
    never read either. Both are now live-read per run (no restart)."""

    def test_disabled_skips_extraction_entirely(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _write_corporate_memory_config(tmp_path, {"sources": {"session_transcripts": {"enabled": False}}})
        from src.repositories.knowledge import KnowledgeRepository

        golden = {
            "verifications": [
                {
                    "detection_type": "correction",
                    "title": "Should never be extracted",
                    "content": "x",
                    "user_quote": "no, it's actually x",
                    "domain": "engineering",
                    "entities": [],
                }
            ]
        }
        extractor = _mock_extractor(golden)

        session_dir = tmp_path / "user_sessions" / "someone"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(json.dumps({"role": "user", "content": "hi"}) + "\n")

        _run_verification_processor(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        # The LLM call itself (the "extraction") must never fire.
        extractor.extract_json.assert_not_called()
        repo = KnowledgeRepository(conn)
        assert repo.list_items(source_type="user_verification") == []
        conn.close()

    def test_detection_types_filters_before_insert(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        _write_corporate_memory_config(
            tmp_path, {"sources": {"session_transcripts": {"detection_types": ["correction"]}}}
        )
        from src.repositories.knowledge import KnowledgeRepository

        golden = {
            "verifications": [
                {
                    "detection_type": "correction",
                    "title": "Kept",
                    "content": "kept content",
                    "user_quote": "no, it's actually x",
                    "domain": "engineering",
                    "entities": [],
                },
                {
                    "detection_type": "confirmation",
                    "title": "Dropped",
                    "content": "dropped content",
                    "user_quote": "yes exactly",
                    "domain": "engineering",
                    "entities": [],
                },
            ]
        }
        extractor = _mock_extractor(golden)

        session_dir = tmp_path / "user_sessions" / "someone"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(json.dumps({"role": "user", "content": "hi"}) + "\n")

        _run_verification_processor(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        repo = KnowledgeRepository(conn)
        items = repo.list_items(source_type="user_verification")
        assert [i["title"] for i in items] == ["Kept"]
        conn.close()

    def test_default_config_extracts_every_detection_type(self, tmp_path, monkeypatch):
        """No corporate_memory config at all (legacy mode) must keep every
        detection_type the LLM can return — unchanged from before this knob
        was wired."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        golden = {
            "verifications": [
                {
                    "detection_type": dt,
                    "title": f"item-{dt}",
                    "content": f"content-{dt}",
                    "user_quote": "q",
                    "domain": "engineering",
                    "entities": [],
                }
                for dt in ("correction", "confirmation", "unprompted_definition")
            ]
        }
        extractor = _mock_extractor(golden)

        session_dir = tmp_path / "user_sessions" / "someone"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(json.dumps({"role": "user", "content": "hi"}) + "\n")

        _run_verification_processor(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        repo = KnowledgeRepository(conn)
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 3
        conn.close()


class TestMaxTurnsPerSessionKnob:
    """issue #1971 Part 6: corporate_memory.sources.session_transcripts.
    max_turns_per_session was documented but never read — the truncation
    window stayed pinned to the hardcoded MAX_TURNS_PER_SESSION constant
    regardless of what an operator set here."""

    def test_config_value_is_passed_through_to_extract_verifications(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "app.instance_config.get_corporate_memory_config",
            lambda: {"sources": {"session_transcripts": {"max_turns_per_session": 3}}},
        )

        captured = {}
        import services.session_processors.verification as verification_module

        def _fake_extract_verifications(extractor, username, session_id, turns, max_turns=100):
            captured["max_turns"] = max_turns
            return []

        monkeypatch.setattr(verification_module, "extract_verifications", _fake_extract_verifications)

        session_dir = tmp_path / "user_sessions" / "eve"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(json.dumps({"role": "user", "content": "hi"}) + "\n")

        _run_verification_processor(
            conn, _mock_extractor({"verifications": []}), session_data_dir=tmp_path / "user_sessions"
        )

        assert captured["max_turns"] == 3
        conn.close()

    def test_unset_keeps_the_built_in_default(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        monkeypatch.setattr("app.instance_config.get_corporate_memory_config", lambda: {})

        captured = {}
        import services.session_processors.verification as verification_module
        from services.verification_detector.detector import MAX_TURNS_PER_SESSION

        def _fake_extract_verifications(extractor, username, session_id, turns, max_turns=100):
            captured["max_turns"] = max_turns
            return []

        monkeypatch.setattr(verification_module, "extract_verifications", _fake_extract_verifications)

        session_dir = tmp_path / "user_sessions" / "eve"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(json.dumps({"role": "user", "content": "hi"}) + "\n")

        _run_verification_processor(
            conn, _mock_extractor({"verifications": []}), session_data_dir=tmp_path / "user_sessions"
        )

        assert captured["max_turns"] == MAX_TURNS_PER_SESSION
        conn.close()


class TestBuildVerificationProcessorModelOverride:
    """issue #1971 Part 6: corporate_memory.extraction.model applies to
    BOTH extraction paths — mirrors TestExtractionModelOverrideKnob in
    tests/test_corporate_memory_collector.py for the collector side."""

    def test_corporate_memory_extraction_model_overrides_the_global_ai_model(self, monkeypatch):
        from services.session_processors import verification as verification_module

        monkeypatch.setattr(
            "app.instance_config.load_instance_config",
            lambda: {
                "ai": {"model": "global-model", "provider": "anthropic"},
                "corporate_memory": {"extraction": {"model": "cm-override-model"}},
            },
        )
        captured = []
        monkeypatch.setattr(
            "connectors.llm.create_extractor_from_env_or_config",
            lambda ai_config=None, **kw: captured.append(ai_config) or object(),
        )

        verification_module.build_verification_processor()

        assert captured[0]["model"] == "cm-override-model"
        assert captured[0]["provider"] == "anthropic"

    def test_no_override_leaves_the_global_ai_config_untouched(self, monkeypatch):
        from services.session_processors import verification as verification_module

        monkeypatch.setattr(
            "app.instance_config.load_instance_config",
            lambda: {"ai": {"model": "global-model"}},
        )
        captured = []
        monkeypatch.setattr(
            "connectors.llm.create_extractor_from_env_or_config",
            lambda ai_config=None, **kw: captured.append(ai_config) or object(),
        )

        verification_module.build_verification_processor()

        assert captured[0]["model"] == "global-model"


class TestVerificationPromptExcludesEngagementScopedFacts:
    """#1957: the LLM prompt over-collected one-off facts scoped to a single
    client engagement (a one-off date, price, or correction that belongs on
    that engagement's own record) because nothing told it to exclude them,
    and its confirmation guidance ("domain-specific (not generic)") could be
    misread as "specific to one engagement" rather than "not trivially
    generic". Both are static prompt-text fixes; pin them so they can't
    silently regress.

    #1971 Part 2 relocated this text from the old monolithic
    ``VERIFICATION_EXTRACT_PROMPT`` constant into
    ``DEFAULT_DETECTION_POLICY`` — the seed value for the editable
    ``memory-curator`` agent profile and the fallback
    ``render_verification_prompt`` uses when that profile is missing/empty.
    Same assertions, new home; #1957's fix is the "engagement" bullet these
    tests pin, now phrased as "route, don't drop" (issue #1971 Part 5) rather
    than a silent exclusion — see the updated assertion below.
    """

    def test_prompt_excludes_engagement_scoped_facts(self):
        from services.verification_detector.prompts import DEFAULT_DETECTION_POLICY

        # Whitespace-normalized so line-wrapping inside the policy text can't
        # split a phrase across a fixed-column substring check.
        normalized = " ".join(DEFAULT_DETECTION_POLICY.lower().split())
        assert "engagement" in normalized
        assert "outside this one engagement" in normalized

    def test_prompt_no_longer_conflates_domain_specific_with_generic(self):
        from services.verification_detector.prompts import DEFAULT_DETECTION_POLICY

        normalized = " ".join(DEFAULT_DETECTION_POLICY.lower().split())
        assert "trivially generic" in normalized
        # Old wording could be misread as "specific to one client/engagement"
        # rather than the intended "not trivially generic".
        assert "domain-specific (not generic)" not in normalized

    def test_prompt_routes_engagement_scoped_facts_instead_of_dropping_them(self):
        """#1971 Part 5: an engagement-scoped fact is no longer silently
        excluded — the model must still return it (scope="engagement") so
        deterministic code can route it, never drop it."""
        from services.verification_detector.prompts import DEFAULT_DETECTION_POLICY

        normalized = " ".join(DEFAULT_DETECTION_POLICY.lower().split())
        assert 'scope="engagement"' in normalized
        assert "still return it" in normalized
        assert "still return it" in normalized
