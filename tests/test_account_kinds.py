"""What counts as an account, and who "everyone" reaches (issue #2256).

The three kinds of row in ``users`` answer three different questions, and
mixing them up is what made the Access page print a number that included
its own plumbing. These tests pin the predicate every other surface reads.
"""

from __future__ import annotations

from src.service_accounts import (
    HUMAN_KIND,
    SERVICE_ACCOUNT_KIND,
    SYSTEM_IDENTITY_EMAILS,
    SYSTEM_IDENTITY_KIND,
    is_person,
    is_service_account,
    is_system_identity,
)


def test_a_plain_account_is_a_person():
    assert is_person({"email": "a@example.com", "kind": HUMAN_KIND})


def test_a_row_with_no_kind_column_is_a_person():
    """The DuckDB backend has no ``kind`` column at all, so the key is
    simply absent — and a service account cannot exist there anyway."""
    assert is_person({"email": "a@example.com"})


def test_a_service_account_is_not_a_person():
    row = {"email": "ci-bot@service.local", "kind": SERVICE_ACCOUNT_KIND}
    assert is_service_account(row)
    assert not is_person(row)


def test_a_system_identity_is_not_a_person():
    row = {"email": "memory-curator@system.local", "kind": SYSTEM_IDENTITY_KIND}
    assert is_system_identity(row)
    assert not is_person(row)


def test_a_system_identity_is_recognised_by_address_before_the_backfill():
    """0098 stamps ``kind``, but the row exists on every instance long
    before that migration runs — and on DuckDB there is no column to stamp.
    The address has to be enough on its own."""
    for email in SYSTEM_IDENTITY_EMAILS:
        row = {"email": email.upper(), "kind": HUMAN_KIND}
        assert is_system_identity(row), email
        assert not is_person(row), email


def test_a_system_identity_is_not_a_service_account():
    """They are deliberately different kinds: 'service' refuses an
    interactive token and refuses Admin membership, and two of these three
    need one or the other."""
    row = {"email": "scheduler@system.local", "kind": SYSTEM_IDENTITY_KIND}
    assert not is_service_account(row)


def test_none_is_nobody():
    assert not is_person(None)
    assert not is_service_account(None)
    assert not is_system_identity(None)


def test_the_seeded_addresses_match_the_modules_that_create_them():
    """``src.service_accounts`` owns the list so the repositories and
    migration 0098 can read it without importing app code. If a seed moves
    or a fourth appears, this is what notices."""
    from app.auth.scheduler_token import SCHEDULER_USER_EMAIL
    from app.auth.system_users import (
        MEMORY_CURATOR_USER_EMAIL,
        SEMANTIC_DRAFTER_USER_EMAIL,
    )

    assert set(SYSTEM_IDENTITY_EMAILS) == {
        SCHEDULER_USER_EMAIL,
        SEMANTIC_DRAFTER_USER_EMAIL,
        MEMORY_CURATOR_USER_EMAIL,
    }


def test_the_migration_inlines_the_same_addresses():
    """0098 inlines the constants rather than importing them — a migration
    describes the schema it shipped against. Inlined still has to agree."""
    import importlib.util
    import pathlib

    path = pathlib.Path("migrations/versions/0098_everyone_becomes_a_scope.py")
    spec = importlib.util.spec_from_file_location("_m0098", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert set(mod.SYSTEM_IDENTITY_EMAILS) == set(SYSTEM_IDENTITY_EMAILS)
    assert mod.SYSTEM_IDENTITY_KIND == SYSTEM_IDENTITY_KIND
    assert mod.HUMAN_KIND == HUMAN_KIND
