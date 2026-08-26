"""``resolve_source_connection(source_type)`` — the single entry a per-source
settings resolver calls with no explicit connection (D2.2)."""

import pytest


@pytest.fixture
def seeded_repo(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    repo.create(
        id="sf1",
        name="snowflake",
        source_type="snowflake",
        config={"account": "acme", "user": "svc", "database": "DB", "warehouse": "WH"},
        is_default=True,
    )
    return repo


def test_returns_default_row_for_type(seeded_repo):
    from src.connection_resolver import resolve_source_connection

    row = resolve_source_connection("snowflake")
    assert row is not None
    assert row["id"] == "sf1"


def test_returns_none_when_no_row_registered(seeded_repo):
    from src.connection_resolver import resolve_source_connection

    assert resolve_source_connection("databricks") is None


def test_equivalent_to_resolve_connection_with_no_id(seeded_repo):
    from src.connection_resolver import resolve_connection, resolve_source_connection

    assert resolve_source_connection("snowflake") == resolve_connection("snowflake", None)
