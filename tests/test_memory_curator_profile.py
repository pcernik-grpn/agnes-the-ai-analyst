"""Tests for the seeded ``memory-curator`` agent profile (issue #1971,
Part 1) — config + identity for the EXISTING direct LLM calls the
corporate-memory detectors already make. No agent runtime/sandbox is
spawned; the profile's ``instructions`` field just holds the editable
detection policy text.

Mirrors ``tests/test_auth_system_users.py``'s ``fresh_db`` isolation
fixture and idempotency assertion style.
"""

import tempfile

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        from src.db import close_system_db

        close_system_db()
        yield tmp
        close_system_db()


def test_ensure_memory_curator_user_seeds_a_plain_user(fresh_db):
    from app.auth.system_users import MEMORY_CURATOR_USER_EMAIL, ensure_memory_curator_user

    user = ensure_memory_curator_user()
    assert user["email"] == MEMORY_CURATOR_USER_EMAIL
    assert user["id"]


def test_ensure_memory_curator_user_is_idempotent(fresh_db):
    from app.auth.system_users import MEMORY_CURATOR_USER_EMAIL, ensure_memory_curator_user
    from src.repositories import users_repo

    user1 = ensure_memory_curator_user()
    user2 = ensure_memory_curator_user()
    assert user1["id"] == user2["id"]

    matches = [u for u in users_repo().list_all() if u["email"] == MEMORY_CURATOR_USER_EMAIL]
    assert len(matches) == 1


def test_ensure_memory_curator_agent_profile_seeds_the_slug(fresh_db):
    from app.services.memory_curator_profile import (
        MEMORY_CURATOR_AGENT_SLUG,
        ensure_memory_curator_agent_profile,
    )
    from app.auth.system_users import MEMORY_CURATOR_USER_EMAIL

    agent = ensure_memory_curator_agent_profile()
    assert agent["slug"] == MEMORY_CURATOR_AGENT_SLUG

    from src.repositories import users_repo

    owner = users_repo().get_by_id(agent["owner_user_id"])
    assert owner["email"] == MEMORY_CURATOR_USER_EMAIL


def test_ensure_memory_curator_agent_profile_seeds_the_default_policy_text(fresh_db):
    from app.services.memory_curator_profile import (
        DEFAULT_DETECTION_POLICY,
        ensure_memory_curator_agent_profile,
    )

    agent = ensure_memory_curator_agent_profile()
    assert agent["system_prompt"] == DEFAULT_DETECTION_POLICY


def test_ensure_memory_curator_agent_profile_is_idempotent_and_never_resets_edits(fresh_db):
    """Second call must not clobber an admin edit made between calls — the
    seed is insert-if-absent, exactly like the canonical memory-domain seed
    and the semantic-drafter user."""
    from app.services.memory_curator_profile import ensure_memory_curator_agent_profile
    from src.repositories import agents_repo

    agent1 = ensure_memory_curator_agent_profile()
    agents_repo().update(agent1["id"], system_prompt="edited policy text")

    agent2 = ensure_memory_curator_agent_profile()
    assert agent2["id"] == agent1["id"]
    assert agent2["system_prompt"] == "edited policy text"


def test_get_memory_curator_policy_text_reflects_live_edits(fresh_db):
    from app.services.memory_curator_profile import (
        ensure_memory_curator_agent_profile,
        get_memory_curator_policy_text,
    )
    from src.repositories import agents_repo

    agent = ensure_memory_curator_agent_profile()
    agents_repo().update(agent["id"], system_prompt="a brand new policy")

    assert get_memory_curator_policy_text() == "a brand new policy"


def test_get_memory_curator_policy_text_falls_back_when_profile_missing(fresh_db):
    """No crash, no empty policy, when the profile was never seeded (or the
    seed call itself failed) — the caller (the detector) must still get a
    usable, non-empty policy string."""
    from app.services.memory_curator_profile import (
        DEFAULT_DETECTION_POLICY,
        get_memory_curator_policy_text,
    )

    assert get_memory_curator_policy_text() == DEFAULT_DETECTION_POLICY


def test_get_memory_curator_policy_text_falls_back_when_instructions_blank(fresh_db):
    from app.services.memory_curator_profile import (
        DEFAULT_DETECTION_POLICY,
        ensure_memory_curator_agent_profile,
        get_memory_curator_policy_text,
    )
    from src.repositories import agents_repo

    agent = ensure_memory_curator_agent_profile()
    agents_repo().update(agent["id"], system_prompt="   ")

    assert get_memory_curator_policy_text() == DEFAULT_DETECTION_POLICY
