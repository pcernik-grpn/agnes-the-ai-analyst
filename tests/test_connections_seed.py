"""First-boot seeding: env/yaml -> default connections; idempotent."""

import pytest

from app.connections_seed import seed_default_connections


@pytest.fixture
def fresh_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.repositories import source_connections_repo

    return source_connections_repo()


def test_seeds_keboola_from_env_normalized(fresh_registry, monkeypatch):
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://connection.example.com/")
    monkeypatch.delenv("BIGQUERY_PROJECT", raising=False)
    seed_default_connections()
    row = fresh_registry.get_by_name("keboola")
    assert row["is_default"] is True
    assert row["config"]["stack_url"] == "https://connection.example.com"  # slash gone
    assert row["token_env"] == "KEBOOLA_STORAGE_TOKEN"
    assert fresh_registry.get_by_name("bigquery") is None


def test_seeding_is_idempotent(fresh_registry, monkeypatch):
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://connection.example.com")
    seed_default_connections()
    seed_default_connections()  # second boot
    assert len(fresh_registry.list(source_type="keboola")) == 1


def test_existing_registry_not_overwritten(fresh_registry, monkeypatch):
    fresh_registry.create(
        id="c9",
        name="keboola",
        source_type="keboola",
        config={"stack_url": "https://admin-set.example.com"},
        is_default=True,
    )
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://env-says.example.com")
    seed_default_connections()  # must be a no-op + warn
    assert fresh_registry.get_by_name("keboola")["config"]["stack_url"] == "https://admin-set.example.com"


def test_bigquery_env_set_but_registry_exists_warns_no_create(fresh_registry, monkeypatch, caplog):
    fresh_registry.create(
        id="bq9",
        name="bigquery",
        source_type="bigquery",
        config={"project": "admin-proj"},
        is_default=True,
    )
    monkeypatch.delenv("KEBOOLA_STACK_URL", raising=False)
    monkeypatch.setenv("BIGQUERY_PROJECT", "env-proj")
    with caplog.at_level("WARNING"):
        seed_default_connections()  # must be a no-op + warn
    assert len(fresh_registry.list(source_type="bigquery")) == 1
    assert fresh_registry.get_by_name("bigquery")["config"]["project"] == "admin-proj"
    assert any("BIGQUERY_PROJECT is set" in r.message for r in caplog.records)


def _fake_instance_config(monkeypatch, fake_cfg):
    """Same pattern as tests/test_admin_register_source_type_validation.py's
    csv_instance fixture: snowflake/databricks coordinates live only in
    instance.yaml (no env-var fallback, unlike KEBOOLA_STACK_URL/BIGQUERY_PROJECT),
    so seeding them needs a faked instance config rather than monkeypatch.setenv.
    """
    monkeypatch.setattr("app.instance_config.load_instance_config", lambda **kw: fake_cfg, raising=False)
    monkeypatch.setattr("config.loader.load_instance_config", lambda **kw: fake_cfg, raising=False)
    from app.instance_config import reset_cache

    reset_cache()


def test_seeds_snowflake_from_yaml(fresh_registry, monkeypatch):
    _fake_instance_config(
        monkeypatch,
        {
            "data_source": {
                "snowflake": {
                    "account": "xy12345",
                    "user": "svc_agnes",
                    "database": "ANALYTICS",
                    "warehouse": "COMPUTE_WH",
                    "role": "ANALYST",
                }
            }
        },
    )
    seed_default_connections()
    row = fresh_registry.get_by_name("snowflake")
    assert row["is_default"] is True
    assert row["config"] == {
        "account": "xy12345",
        "user": "svc_agnes",
        "database": "ANALYTICS",
        "warehouse": "COMPUTE_WH",
        "role": "ANALYST",
        "auth_type": "password",
    }
    assert row["token_env"] == "SNOWFLAKE_PASSWORD"


def test_snowflake_key_pair_auth_seeds_private_key_env(fresh_registry, monkeypatch):
    _fake_instance_config(
        monkeypatch,
        {
            "data_source": {
                "snowflake": {
                    "account": "xy12345",
                    "user": "svc_agnes",
                    "database": "ANALYTICS",
                    "warehouse": "COMPUTE_WH",
                    "auth_type": "key_pair",
                    "private_key_env": "MY_SF_KEY",
                }
            }
        },
    )
    seed_default_connections()
    row = fresh_registry.get_by_name("snowflake")
    assert row["config"]["auth_type"] == "key_pair"
    assert row["token_env"] == "MY_SF_KEY"


def test_snowflake_incomplete_config_is_not_seeded(fresh_registry, monkeypatch, caplog):
    # account set, but user/database/warehouse missing — never usable, so
    # seeding it would create a row that resolve_snowflake_settings still
    # treats as unconfigured. Skip with a warning rather than raising.
    _fake_instance_config(monkeypatch, {"data_source": {"snowflake": {"account": "xy12345"}}})
    with caplog.at_level("WARNING"):
        seed_default_connections()
    assert fresh_registry.get_by_name("snowflake") is None
    assert any("snowflake" in r.message.lower() for r in caplog.records)


def test_snowflake_seeding_is_idempotent(fresh_registry, monkeypatch):
    _fake_instance_config(
        monkeypatch,
        {
            "data_source": {
                "snowflake": {
                    "account": "xy12345",
                    "user": "svc_agnes",
                    "database": "ANALYTICS",
                    "warehouse": "COMPUTE_WH",
                }
            }
        },
    )
    seed_default_connections()
    seed_default_connections()  # second boot
    assert len(fresh_registry.list(source_type="snowflake")) == 1


def test_existing_snowflake_registry_not_overwritten(fresh_registry, monkeypatch, caplog):
    fresh_registry.create(
        id="sf9",
        name="snowflake",
        source_type="snowflake",
        config={"account": "admin-set-account", "user": "u", "database": "d", "warehouse": "w"},
        is_default=True,
    )
    _fake_instance_config(
        monkeypatch,
        {
            "data_source": {
                "snowflake": {
                    "account": "yaml-says-account",
                    "user": "u",
                    "database": "d",
                    "warehouse": "w",
                }
            }
        },
    )
    with caplog.at_level("WARNING"):
        seed_default_connections()  # must be a no-op + warn
    assert len(fresh_registry.list(source_type="snowflake")) == 1
    assert fresh_registry.get_by_name("snowflake")["config"]["account"] == "admin-set-account"
    assert any("snowflake" in r.message.lower() for r in caplog.records)


def test_seeds_databricks_from_yaml(fresh_registry, monkeypatch):
    _fake_instance_config(
        monkeypatch,
        {
            "data_source": {
                "databricks": {
                    "host": "https://dbc-a1b2c3d4-e5f6.cloud.databricks.com",
                    "warehouse_id": "abc123",
                    "catalog": "main",
                }
            }
        },
    )
    seed_default_connections()
    row = fresh_registry.get_by_name("databricks")
    assert row["is_default"] is True
    assert row["config"]["host"] == "https://dbc-a1b2c3d4-e5f6.cloud.databricks.com"
    assert row["config"]["warehouse_id"] == "abc123"
    assert row["config"]["catalog"] == "main"
    assert row["token_env"] == "DATABRICKS_TOKEN"


def test_databricks_incomplete_config_is_not_seeded(fresh_registry, monkeypatch, caplog):
    # host set, but warehouse_id missing — never usable.
    _fake_instance_config(monkeypatch, {"data_source": {"databricks": {"host": "https://dbc-x.cloud.databricks.com"}}})
    with caplog.at_level("WARNING"):
        seed_default_connections()
    assert fresh_registry.get_by_name("databricks") is None
    assert any("databricks" in r.message.lower() for r in caplog.records)


def test_databricks_seeding_is_idempotent(fresh_registry, monkeypatch):
    _fake_instance_config(
        monkeypatch,
        {
            "data_source": {
                "databricks": {
                    "host": "https://dbc-a1b2c3d4-e5f6.cloud.databricks.com",
                    "warehouse_id": "abc123",
                }
            }
        },
    )
    seed_default_connections()
    seed_default_connections()  # second boot
    assert len(fresh_registry.list(source_type="databricks")) == 1


def test_existing_databricks_registry_not_overwritten(fresh_registry, monkeypatch, caplog):
    fresh_registry.create(
        id="dbx9",
        name="databricks",
        source_type="databricks",
        config={"host": "https://admin-set.cloud.databricks.com", "warehouse_id": "admin-wh"},
        is_default=True,
    )
    _fake_instance_config(
        monkeypatch,
        {
            "data_source": {
                "databricks": {
                    "host": "https://yaml-says.cloud.databricks.com",
                    "warehouse_id": "yaml-wh",
                }
            }
        },
    )
    with caplog.at_level("WARNING"):
        seed_default_connections()  # must be a no-op + warn
    assert len(fresh_registry.list(source_type="databricks")) == 1
    assert fresh_registry.get_by_name("databricks")["config"]["host"] == "https://admin-set.cloud.databricks.com"
    assert any("databricks" in r.message.lower() for r in caplog.records)
