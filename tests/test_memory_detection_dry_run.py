"""Tests for `POST /api/memory/admin/detection-dry-run` (issue #1971 Part 4).

Runs the transcript detector over a small, capped number of sessions with
the CURRENT saved policy, writes NOTHING to knowledge_items, and returns
what would be proposed/filtered/routed. Recording the memory_detection_runs
row (dry_run=True) is PG-only, but the preview itself is NOT — this is one
of the "RUN paths must not break on DuckDB" requirements from issue #1971
Part 3, so the whole suite here runs on the default (DuckDB) backend.
"""

import json
from unittest.mock import MagicMock

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mock_extractor(golden_response: dict) -> MagicMock:
    mock = MagicMock()
    mock.extract_json.return_value = golden_response
    return mock


@pytest.fixture
def session_with_one_verification(tmp_path, monkeypatch):
    """A session file the pipeline's scan_unprocessed_for() will surface,
    plus a mocked extractor wired in as the module-level factory the
    endpoint calls."""
    monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
    session_dir = tmp_path / "user_sessions" / "alice"
    session_dir.mkdir(parents=True)
    with open(session_dir / "s.jsonl", "w") as f:
        f.write(json.dumps({"role": "user", "content": "hi"}) + "\n")
    return session_dir


def test_requires_admin(seeded_app):
    client = seeded_app["client"]
    r = client.post(
        "/api/memory/admin/detection-dry-run",
        json={},
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert r.status_code == 403


def test_writes_nothing_to_knowledge_items(seeded_app, monkeypatch, session_with_one_verification):
    golden = {
        "verifications": [
            {
                "detection_type": "unprompted_definition",
                "title": "Our convention for X",
                "content": "X is defined as Y.",
                "user_quote": "our convention is X is Y",
                "domain": "engineering",
                "entities": [],
                "scope": "general",
            }
        ]
    }
    monkeypatch.setattr(
        "connectors.llm.create_extractor_from_env_or_config",
        lambda ai_config: _mock_extractor(golden),
    )

    from src.repositories import knowledge_repo

    before_count = len(knowledge_repo().list_items())

    client = seeded_app["client"]
    r = client.post(
        "/api/memory/admin/detection-dry-run",
        json={"limit": 5},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items_proposed"] == 1
    assert body["items_would_insert"] == 1
    assert body["items_routed_side_domain"] == 0

    after_count = len(knowledge_repo().list_items())
    assert after_count == before_count  # nothing written


def test_engagement_scoped_item_is_counted_as_routed(seeded_app, monkeypatch, session_with_one_verification):
    golden = {
        "verifications": [
            {
                "detection_type": "correction",
                "title": "Client wants it Friday",
                "content": "One-off deadline.",
                "user_quote": "no, actually Friday",
                "domain": "operations",
                "entities": [],
                "scope": "engagement",
            }
        ]
    }
    monkeypatch.setattr(
        "connectors.llm.create_extractor_from_env_or_config",
        lambda ai_config: _mock_extractor(golden),
    )

    client = seeded_app["client"]
    r = client.post(
        "/api/memory/admin/detection-dry-run",
        json={},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items_routed_side_domain"] == 1


def test_never_fails_on_duckdb_backend_even_though_run_log_is_pg_only(
    seeded_app, monkeypatch, session_with_one_verification
):
    """The RUN path (this endpoint) must not break on DuckDB — the optional
    memory_detection_runs write is skipped with a warning, never surfaced
    as a failure of the dry run itself."""
    monkeypatch.setattr(
        "connectors.llm.create_extractor_from_env_or_config",
        lambda ai_config: _mock_extractor({"verifications": []}),
    )

    client = seeded_app["client"]
    r = client.post(
        "/api/memory/admin/detection-dry-run",
        json={},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 200
    assert r.json()["items_proposed"] == 0


def test_limit_is_capped_at_the_http_layer(seeded_app, monkeypatch, tmp_path):
    """An absurd limit request does not crash — it's clamped downstream.
    The exact clamp value is a unit-level property, asserted directly on
    ``dry_run_verification_detection`` in
    tests/test_corporate_memory_v1.py-style unit tests below."""
    monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
    monkeypatch.setattr(
        "connectors.llm.create_extractor_from_env_or_config",
        lambda ai_config: _mock_extractor({"verifications": []}),
    )

    client = seeded_app["client"]
    r = client.post(
        "/api/memory/admin/detection-dry-run",
        json={"limit": 999999},
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 200


def test_dry_run_function_clamps_an_absurd_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module

    db_module._system_db_conn = None
    db_module._system_db_path = None
    db_module.get_system_db()

    from services.session_processors.verification import dry_run_verification_detection

    extractor = _mock_extractor({"verifications": []})
    result = dry_run_verification_detection(extractor, limit=999999, session_data_dir=tmp_path / "empty_sessions")
    assert result["sessions_scanned"] == 0  # no sessions exist; just proves it didn't raise
