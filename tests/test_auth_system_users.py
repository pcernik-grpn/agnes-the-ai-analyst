"""Tests for the semantic-drafter system identity (semantic-phase5, wave 1,
Task 2) — a plain, NON-admin user row wave 2's headless auto-drafting
session authenticates as.

Mirrors ``tests/test_auth_scheduler_token.py``'s ``fresh_db`` isolation
fixture and idempotency assertion style.
"""

import tempfile

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    """Isolated DuckDB per test, mirroring tests/test_auth_scheduler_token.py."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        from src.db import close_system_db

        close_system_db()
        yield tmp
        close_system_db()


def test_ensure_semantic_drafter_user_seeds_a_plain_user(fresh_db):
    from app.auth.system_users import SEMANTIC_DRAFTER_USER_EMAIL, ensure_semantic_drafter_user

    user = ensure_semantic_drafter_user()
    assert user["email"] == SEMANTIC_DRAFTER_USER_EMAIL
    assert user["id"]


def test_ensure_semantic_drafter_user_is_idempotent(fresh_db):
    """First call seeds; second call returns the same row, no duplicate."""
    from app.auth.system_users import ensure_semantic_drafter_user
    from src.repositories import users_repo

    user1 = ensure_semantic_drafter_user()
    user2 = ensure_semantic_drafter_user()
    assert user1["id"] == user2["id"]

    from app.auth.system_users import SEMANTIC_DRAFTER_USER_EMAIL

    matches = [u for u in users_repo().list_all() if u["email"] == SEMANTIC_DRAFTER_USER_EMAIL]
    assert len(matches) == 1


def test_ensure_semantic_drafter_user_is_not_an_admin(fresh_db):
    """Critical difference from ensure_scheduler_user: this identity must
    stay a plain, non-admin user — POST /api/semantic-models/apply routes a
    non-admin caller to the submitted_for_review moderation queue instead of
    a direct write, and that safety property depends on this identity never
    holding Admin group membership.
    """
    from app.auth.system_users import ensure_semantic_drafter_user
    from src.repositories import user_group_members_repo

    user = ensure_semantic_drafter_user()
    group_names = user_group_members_repo().list_group_names_for_user(user["id"])
    assert "Admin" not in group_names
