"""Postgres-only tests for the SSO login repositories (design 2026-08-28).

``sso_config`` (singleton, Fernet-encrypted client secret column) and
``user_external_identities`` (validated-token ``tid``/``oid`` bindings).
There is no DuckDB half to parametrize against (PG-first ratchet, A3) — see
``docs/migrations.md`` -> "Adding a PG-only feature". Pattern follows
``tests/db_pg/test_corpus_file_sources_pg.py``'s PG construction helper.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_repos(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    from src import db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.sso_config_pg import SsoConfigPgRepository
    from src.repositories.user_external_identities_pg import (
        UserExternalIdentitiesPgRepository,
    )

    return (
        SsoConfigPgRepository(db_pg.get_engine()),
        UserExternalIdentitiesPgRepository(db_pg.get_engine()),
    )


def _add_user(pg_engine, user_id: str, email: str) -> str:
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": user_id, "email": email},
        )
    return user_id


def _upsert_default_config(repo, **overrides):
    kwargs = {
        "tenant_id": "11111111-2222-3333-4444-555555555555",
        "client_id": "app-client-id",
        "display_name": "Fabrikam",
        "allowed_email_domains": ["fabrikam.com"],
        "enabled": False,
        "updated_by": "admin-1",
    }
    kwargs.update(overrides)
    repo.upsert_config(**kwargs)
    return kwargs


# ---------------------------------------------------------------------------
# sso_config
# ---------------------------------------------------------------------------


def test_get_config_returns_none_when_unconfigured(pg_engine, monkeypatch):
    cfg_repo, _ = _make_repos(pg_engine, monkeypatch)
    assert cfg_repo.get_config() is None


def test_upsert_then_get_config_round_trips_without_secret_column(pg_engine, monkeypatch):
    cfg_repo, _ = _make_repos(pg_engine, monkeypatch)
    _upsert_default_config(cfg_repo)
    cfg = cfg_repo.get_config()
    assert cfg is not None
    assert cfg["provider_type"] == "entra_oidc"
    assert cfg["tenant_id"] == "11111111-2222-3333-4444-555555555555"
    assert cfg["client_id"] == "app-client-id"
    assert cfg["display_name"] == "Fabrikam"
    assert cfg["allowed_email_domains"] == ["fabrikam.com"]
    assert cfg["enabled"] is False
    assert cfg["has_client_secret"] is False
    assert cfg["updated_by"] == "admin-1"
    assert cfg["updated_at"] is not None
    # Write-only discipline: the ciphertext never leaves the repo via get_config.
    assert "client_secret_enc" not in cfg


def test_upsert_config_normalizes_domains_at_write(pg_engine, monkeypatch):
    cfg_repo, _ = _make_repos(pg_engine, monkeypatch)
    _upsert_default_config(
        cfg_repo,
        allowed_email_domains=["  Fabrikam.COM ", "fabrikam.com", "", "Partners.Fabrikam.com"],
    )
    cfg = cfg_repo.get_config()
    assert cfg["allowed_email_domains"] == ["fabrikam.com", "partners.fabrikam.com"]


def test_upsert_config_updates_in_place(pg_engine, monkeypatch):
    cfg_repo, _ = _make_repos(pg_engine, monkeypatch)
    _upsert_default_config(cfg_repo)
    _upsert_default_config(
        cfg_repo,
        display_name="Fabrikam Corp",
        enabled=True,
        updated_by="admin-2",
    )
    cfg = cfg_repo.get_config()
    assert cfg["display_name"] == "Fabrikam Corp"
    assert cfg["enabled"] is True
    assert cfg["updated_by"] == "admin-2"
    # Still a single row — the singleton did not fork.
    with pg_engine.connect() as conn:
        n = conn.execute(sa.text("SELECT COUNT(*) FROM sso_config")).scalar()
    assert n == 1


def test_singleton_check_rejects_non_default_id(pg_engine, monkeypatch):
    _make_repos(pg_engine, monkeypatch)
    with pytest.raises(sa.exc.IntegrityError), pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO sso_config "
                "(id, provider_type, tenant_id, client_id, display_name, allowed_email_domains) "
                "VALUES ('second', 'entra_oidc', 't', 'c', 'X', 'x.com')"
            )
        )


def test_provider_type_check_rejects_unknown_value(pg_engine, monkeypatch):
    _make_repos(pg_engine, monkeypatch)
    with pytest.raises(sa.exc.IntegrityError), pg_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO sso_config "
                "(id, provider_type, tenant_id, client_id, display_name, allowed_email_domains) "
                "VALUES ('default', 'saml', 't', 'c', 'X', 'x.com')"
            )
        )


def test_client_secret_round_trip(pg_engine, monkeypatch):
    cfg_repo, _ = _make_repos(pg_engine, monkeypatch)
    _upsert_default_config(cfg_repo)
    assert cfg_repo.get_client_secret() is None
    assert cfg_repo.set_client_secret("s3cret-value") is True
    assert cfg_repo.get_client_secret() == "s3cret-value"
    assert cfg_repo.get_config()["has_client_secret"] is True
    # The stored column is ciphertext, not the plaintext.
    with pg_engine.connect() as conn:
        raw = conn.execute(sa.text("SELECT client_secret_enc FROM sso_config")).scalar()
    assert raw is not None
    assert "s3cret-value" not in raw


def test_set_client_secret_without_config_row_returns_false(pg_engine, monkeypatch):
    cfg_repo, _ = _make_repos(pg_engine, monkeypatch)
    assert cfg_repo.set_client_secret("whatever") is False


def test_decrypt_failure_reads_as_unset(pg_engine, monkeypatch):
    """A rotated/malformed vault key must read as 'no secret' — never raise."""
    cfg_repo, _ = _make_repos(pg_engine, monkeypatch)
    _upsert_default_config(cfg_repo)
    cfg_repo.set_client_secret("s3cret-value")
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    assert cfg_repo.get_client_secret() is None
    # has_client_secret reflects row presence, not decryptability.
    assert cfg_repo.get_config()["has_client_secret"] is True


def test_clear_client_secret(pg_engine, monkeypatch):
    cfg_repo, _ = _make_repos(pg_engine, monkeypatch)
    _upsert_default_config(cfg_repo)
    cfg_repo.set_client_secret("s3cret-value")
    assert cfg_repo.clear_client_secret() is True
    assert cfg_repo.get_client_secret() is None
    assert cfg_repo.get_config()["has_client_secret"] is False
    # Clearing with no config row present reports False.
    cfg_repo.delete_config()
    assert cfg_repo.clear_client_secret() is False


def test_set_enabled(pg_engine, monkeypatch):
    cfg_repo, _ = _make_repos(pg_engine, monkeypatch)
    _upsert_default_config(cfg_repo)
    assert cfg_repo.set_enabled(True, updated_by="admin-2") is True
    assert cfg_repo.get_config()["enabled"] is True
    assert cfg_repo.get_config()["updated_by"] == "admin-2"
    assert cfg_repo.set_enabled(False, updated_by="admin-3") is True
    assert cfg_repo.get_config()["enabled"] is False


def test_set_enabled_without_config_row_returns_false(pg_engine, monkeypatch):
    cfg_repo, _ = _make_repos(pg_engine, monkeypatch)
    assert cfg_repo.set_enabled(True, updated_by="admin-1") is False


def test_delete_config_removes_row_and_secret_atomically(pg_engine, monkeypatch):
    cfg_repo, _ = _make_repos(pg_engine, monkeypatch)
    _upsert_default_config(cfg_repo)
    cfg_repo.set_client_secret("s3cret-value")
    assert cfg_repo.delete_config() is True
    assert cfg_repo.get_config() is None
    assert cfg_repo.get_client_secret() is None
    assert cfg_repo.delete_config() is False


def test_upsert_config_preserves_stored_secret(pg_engine, monkeypatch):
    """Re-saving the config form must not clear an already-stored secret."""
    cfg_repo, _ = _make_repos(pg_engine, monkeypatch)
    _upsert_default_config(cfg_repo)
    cfg_repo.set_client_secret("s3cret-value")
    _upsert_default_config(cfg_repo, display_name="Renamed")
    assert cfg_repo.get_client_secret() == "s3cret-value"


# ---------------------------------------------------------------------------
# user_external_identities
# ---------------------------------------------------------------------------

TID = "99999999-8888-7777-6666-555555555555"


def _link_kwargs(user_id: str, **overrides):
    kwargs = {
        "user_id": user_id,
        "provider_type": "entra_oidc",
        "tenant_id": TID,
        "subject": "oid-user-1",
        "email_at_link": "user1@fabrikam.com",
    }
    kwargs.update(overrides)
    return kwargs


def test_get_by_user_id_returns_none_when_unlinked(pg_engine, monkeypatch):
    _, ids_repo = _make_repos(pg_engine, monkeypatch)
    assert ids_repo.get_by_user_id("u-missing") is None


def test_link_then_lookup_round_trips(pg_engine, monkeypatch):
    _, ids_repo = _make_repos(pg_engine, monkeypatch)
    _add_user(pg_engine, "u1", "user1@fabrikam.com")
    ids_repo.link(**_link_kwargs("u1"))

    by_user = ids_repo.get_by_user_id("u1")
    assert by_user is not None
    assert by_user["provider_type"] == "entra_oidc"
    assert by_user["tenant_id"] == TID
    assert by_user["subject"] == "oid-user-1"
    assert by_user["email_at_link"] == "user1@fabrikam.com"
    assert by_user["linked_at"] is not None
    assert by_user["last_login_at"] is None

    by_subject = ids_repo.get_by_subject("entra_oidc", TID, "oid-user-1")
    assert by_subject is not None
    assert by_subject["user_id"] == "u1"


def test_link_same_subject_to_second_user_raises_conflict(pg_engine, monkeypatch):
    from src.repositories.user_external_identities_pg import IdentityLinkConflictError

    _, ids_repo = _make_repos(pg_engine, monkeypatch)
    _add_user(pg_engine, "u1", "user1@fabrikam.com")
    _add_user(pg_engine, "u2", "user2@fabrikam.com")
    ids_repo.link(**_link_kwargs("u1"))
    with pytest.raises(IdentityLinkConflictError):
        ids_repo.link(**_link_kwargs("u2", email_at_link="user2@fabrikam.com"))


def test_link_second_identity_for_user_without_replace_raises_conflict(pg_engine, monkeypatch):
    from src.repositories.user_external_identities_pg import IdentityLinkConflictError

    _, ids_repo = _make_repos(pg_engine, monkeypatch)
    _add_user(pg_engine, "u1", "user1@fabrikam.com")
    ids_repo.link(**_link_kwargs("u1"))
    with pytest.raises(IdentityLinkConflictError):
        ids_repo.link(**_link_kwargs("u1", subject="oid-other"))


def test_link_replace_existing_swaps_the_binding(pg_engine, monkeypatch):
    """Stale-binding replacement after a tenant re-point (design: binding
    algorithm, replacement rule)."""
    _, ids_repo = _make_repos(pg_engine, monkeypatch)
    _add_user(pg_engine, "u1", "user1@fabrikam.com")
    ids_repo.link(**_link_kwargs("u1"))
    ids_repo.touch_last_login("u1")
    assert ids_repo.get_by_user_id("u1")["last_login_at"] is not None

    other_tid = "00000000-0000-0000-0000-000000000001"
    ids_repo.link(
        **_link_kwargs("u1", tenant_id=other_tid, subject="oid-new", email_at_link="u1@new.example"),
        replace_existing=True,
    )
    row = ids_repo.get_by_user_id("u1")
    assert row["tenant_id"] == other_tid
    assert row["subject"] == "oid-new"
    assert row["email_at_link"] == "u1@new.example"
    # A replacement is a NEW binding: last_login_at resets until the next touch.
    assert row["last_login_at"] is None
    # The old subject key no longer resolves.
    assert ids_repo.get_by_subject("entra_oidc", TID, "oid-user-1") is None


def test_link_replace_existing_still_raises_on_foreign_subject(pg_engine, monkeypatch):
    from src.repositories.user_external_identities_pg import IdentityLinkConflictError

    _, ids_repo = _make_repos(pg_engine, monkeypatch)
    _add_user(pg_engine, "u1", "user1@fabrikam.com")
    _add_user(pg_engine, "u2", "user2@fabrikam.com")
    ids_repo.link(**_link_kwargs("u1"))
    with pytest.raises(IdentityLinkConflictError):
        ids_repo.link(
            **_link_kwargs("u2", email_at_link="user2@fabrikam.com"),
            replace_existing=True,
        )


def test_touch_last_login(pg_engine, monkeypatch):
    _, ids_repo = _make_repos(pg_engine, monkeypatch)
    _add_user(pg_engine, "u1", "user1@fabrikam.com")
    ids_repo.link(**_link_kwargs("u1"))
    assert ids_repo.get_by_user_id("u1")["last_login_at"] is None
    ids_repo.touch_last_login("u1")
    assert ids_repo.get_by_user_id("u1")["last_login_at"] is not None


def test_unlink(pg_engine, monkeypatch):
    _, ids_repo = _make_repos(pg_engine, monkeypatch)
    _add_user(pg_engine, "u1", "user1@fabrikam.com")
    ids_repo.link(**_link_kwargs("u1"))
    assert ids_repo.unlink("u1") is True
    assert ids_repo.get_by_user_id("u1") is None
    assert ids_repo.unlink("u1") is False


def test_list_page_and_count(pg_engine, monkeypatch):
    _, ids_repo = _make_repos(pg_engine, monkeypatch)
    for i in range(5):
        _add_user(pg_engine, f"u{i}", f"user{i}@fabrikam.com")
        ids_repo.link(**_link_kwargs(f"u{i}", subject=f"oid-{i}", email_at_link=f"user{i}@fabrikam.com"))

    assert ids_repo.count() == 5
    page = ids_repo.list_page(limit=2, offset=0)
    assert len(page) == 2
    assert {"user_id", "provider_type", "tenant_id", "subject", "email_at_link", "linked_at", "last_login_at"} <= set(
        page[0].keys()
    )
    # The bound user's CURRENT email rides along for the admin list.
    assert page[0]["email"].endswith("@fabrikam.com")
    rest = ids_repo.list_page(limit=10, offset=2)
    assert len(rest) == 3
    # Ordered linked_at DESC — no row repeats across pages.
    all_ids = {r["user_id"] for r in page} | {r["user_id"] for r in rest}
    assert all_ids == {f"u{i}" for i in range(5)}
    # Offset past the end is an empty page, not an error.
    assert ids_repo.list_page(limit=10, offset=99) == []


def test_deleting_user_cascades_identity_row(pg_engine, monkeypatch):
    _, ids_repo = _make_repos(pg_engine, monkeypatch)
    _add_user(pg_engine, "u1", "user1@fabrikam.com")
    ids_repo.link(**_link_kwargs("u1"))
    with pg_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM users WHERE id = 'u1'"))
    assert ids_repo.get_by_user_id("u1") is None
