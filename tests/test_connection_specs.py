import pytest

from src.connection_specs import validate_connection_config


def test_keboola_normalizes_trailing_slash():
    cfg = validate_connection_config("keboola", {"stack_url": "https://connection.example.com/"})
    assert cfg["stack_url"] == "https://connection.example.com"


def test_keboola_requires_https_stack_url():
    with pytest.raises(ValueError, match="stack_url"):
        validate_connection_config("keboola", {})
    with pytest.raises(ValueError, match="https"):
        validate_connection_config("keboola", {"stack_url": "ftp://x"})


def test_bigquery_requires_project_defaults_location():
    cfg = validate_connection_config("bigquery", {"project": "my-proj"})
    assert cfg["location"] == "us"
    with pytest.raises(ValueError, match="project"):
        validate_connection_config("bigquery", {})


def test_unknown_source_type_rejected():
    with pytest.raises(ValueError, match="unknown source_type"):
        validate_connection_config("oracle", {})


def test_snowflake_requires_account_user_database_warehouse():
    cfg = validate_connection_config(
        "snowflake",
        {
            "account": "xy12345",
            "user": "svc_agnes",
            "database": "ANALYTICS",
            "warehouse": "COMPUTE_WH",
        },
    )
    # role/auth_type get sane defaults when omitted, mirroring
    # resolve_snowflake_settings' own defaulting.
    assert cfg["role"] == ""
    assert cfg["auth_type"] == "password"

    for missing in ("account", "user", "database", "warehouse"):
        full = {
            "account": "xy12345",
            "user": "svc_agnes",
            "database": "ANALYTICS",
            "warehouse": "COMPUTE_WH",
        }
        full.pop(missing)
        with pytest.raises(ValueError, match=missing):
            validate_connection_config("snowflake", full)


def test_snowflake_rejects_unknown_auth_type():
    with pytest.raises(ValueError, match="auth_type"):
        validate_connection_config(
            "snowflake",
            {
                "account": "xy12345",
                "user": "svc_agnes",
                "database": "ANALYTICS",
                "warehouse": "COMPUTE_WH",
                "auth_type": "oauth",
            },
        )


def test_snowflake_passes_through_secret_ref_fields():
    cfg = validate_connection_config(
        "snowflake",
        {
            "account": "xy12345",
            "user": "svc_agnes",
            "database": "ANALYTICS",
            "warehouse": "COMPUTE_WH",
            "role": "ANALYST",
            "auth_type": "key_pair",
            "private_key_env": "MY_SF_KEY",
            "private_key_passphrase_env": "MY_SF_PASSPHRASE",
        },
    )
    assert cfg["role"] == "ANALYST"
    assert cfg["private_key_env"] == "MY_SF_KEY"
    assert cfg["private_key_passphrase_env"] == "MY_SF_PASSPHRASE"
