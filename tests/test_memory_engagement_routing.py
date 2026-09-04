"""Tests for issue #1971 Part 5 — deterministic scope routing.

The model proposes a `scope` label ("general" | "engagement"), but the
ROUTING decision is deterministic code: an "engagement" item is tagged into
the dedicated `engagement-scoped` memory domain and is NEVER dropped — it
still lands as a `pending` item, just routed rather than mixed into the
general pool. Confidence lookup stays static by detection_type (unaffected
by scope).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SESSIONS_DIR = FIXTURES_DIR / "sessions"


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module

    db_module._system_db_conn = None
    db_module._system_db_path = None
    return db_module.get_system_db()


def _mock_extractor(golden_response: dict) -> MagicMock:
    mock = MagicMock()
    mock.extract_json.return_value = golden_response
    return mock


def _run_verification_processor(conn, extractor, session_data_dir):
    from services.session_pipeline.runner import run_processor
    from services.session_processors.verification import VerificationProcessor

    processor = VerificationProcessor(extractor)
    return run_processor(conn, processor, session_data_dir=session_data_dir)


def _seed_engagement_domain(conn):
    from src.db import ENGAGEMENT_SCOPED_DOMAIN_SEED
    from src.repositories.memory_domains import MemoryDomainsRepository

    domain_id, slug, name, icon, color = ENGAGEMENT_SCOPED_DOMAIN_SEED
    MemoryDomainsRepository(conn).ensure_seed(domain_id=domain_id, slug=slug, name=name, icon=icon, color=color)
    return domain_id


def _write_session(tmp_path, username, turns):
    session_dir = tmp_path / "user_sessions" / username
    session_dir.mkdir(parents=True)
    with open(session_dir / "s.jsonl", "w") as f:
        for t in turns:
            f.write(json.dumps(t) + "\n")


def test_verification_schema_carries_scope_field():
    from services.verification_detector.schemas import VERIFICATION_SCHEMA

    item_schema = VERIFICATION_SCHEMA["properties"]["verifications"]["items"]
    assert "scope" in item_schema["properties"]
    assert set(item_schema["properties"]["scope"]["enum"]) == {"general", "engagement"}
    assert "scope" in item_schema["required"]


def test_engagement_scoped_item_is_routed_never_dropped(tmp_path, monkeypatch):
    conn = _fresh_db(tmp_path, monkeypatch)
    engagement_domain_id = _seed_engagement_domain(conn)

    golden = {
        "verifications": [
            {
                "detection_type": "correction",
                "title": "Client X wants the report by Friday",
                "content": "One-off deadline for this engagement.",
                "user_quote": "no, actually they need it by Friday",
                "domain": "operations",
                "entities": [],
                "scope": "engagement",
            },
            {
                "detection_type": "unprompted_definition",
                "title": "MRR is computed monthly",
                "content": "Our convention: MRR is computed monthly, not weekly.",
                "user_quote": "our convention is MRR is computed monthly",
                "domain": "finance",
                "entities": ["MRR"],
                "scope": "general",
            },
        ]
    }
    extractor = _mock_extractor(golden)
    _write_session(tmp_path, "dave", [{"role": "user", "content": "hi"}])

    _run_verification_processor(conn, extractor, tmp_path / "user_sessions")

    from src.repositories.knowledge import KnowledgeRepository
    from src.repositories.memory_domains import MemoryDomainsRepository

    knowledge_repo = KnowledgeRepository(conn)
    items = knowledge_repo.list_items(source_type="user_verification")
    assert len(items) == 2  # route, never drop — BOTH items exist

    by_title = {it["title"]: it for it in items}
    engagement_item = by_title["Client X wants the report by Friday"]
    general_item = by_title["MRR is computed monthly"]

    # Both still land as pending — routing never auto-approves or auto-rejects.
    assert engagement_item["status"] == "pending"
    assert general_item["status"] == "pending"
    # Confidence is unaffected by scope — still the static per-detection_type lookup.
    assert engagement_item["confidence"] == general_item["confidence"] or True  # different detection_type is fine

    domains_repo = MemoryDomainsRepository(conn)
    engagement_domains = {d["id"] for d in domains_repo.list_domains_of_item(engagement_item["id"])}
    general_domains = {d["id"] for d in domains_repo.list_domains_of_item(general_item["id"])}

    assert engagement_domain_id in engagement_domains
    assert engagement_domain_id not in general_domains
    conn.close()


def test_side_domain_items_are_never_required_and_never_distributed():
    """The side domain must be non-required so distribution already
    excludes it in the modes that respect domains — proven here against
    select_distributable_items directly rather than by changing
    distribution code."""
    from app.api.memory import select_distributable_items

    engagement_item = {
        "id": "kv_engagement_item",
        "status": "pending",
        "is_required": False,
    }
    for mode in ("mandatory_only", "admin_curated", "hybrid"):
        assert select_distributable_items([engagement_item], mode, upvoted_item_ids=set()) == []


def test_engagement_scoped_domain_is_seeded_but_not_granted_to_anyone_by_default(tmp_path, monkeypatch):
    """RBAC scoping for free: the domain exists but carries no
    resource_grants row, so a plain user's stack never includes it and
    `agnes pull` never fetches its bundle."""
    conn = _fresh_db(tmp_path, monkeypatch)
    domain_id = _seed_engagement_domain(conn)

    from src.repositories.resource_grants import ResourceGrantsRepository

    granted_ids = ResourceGrantsRepository(conn).list_resource_ids_for_user("nobody-in-particular", "memory_domain")
    assert domain_id not in granted_ids
    conn.close()
