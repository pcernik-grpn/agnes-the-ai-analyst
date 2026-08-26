"""First-boot seeding of default source connections (spec 2026-06-12 §3.4).

One-time: if no connection of a given source_type exists, seed it from
today's env vars / instance.yaml. Afterwards the registry is the sole
source of truth; a set-but-ignored env var earns a deprecation WARNING
(step 1 of the three-step env retirement).
"""

from __future__ import annotations

import logging
import os
import uuid

logger = logging.getLogger(__name__)


def _yaml_value(*path: str) -> str:
    try:
        from app.instance_config import get_value

        return str(get_value(*path, default="") or "")
    except Exception:
        return ""


def seed_default_connections() -> None:
    from src.connection_specs import validate_connection_config
    from src.repositories import source_connections_repo

    repo = source_connections_repo()

    # --- keboola ---
    stack_url = os.environ.get("KEBOOLA_STACK_URL", "") or _yaml_value("data_source", "keboola", "stack_url")
    existing = repo.list(source_type="keboola")
    if existing:
        if stack_url and all(r["config"].get("stack_url") != stack_url.rstrip("/") for r in existing):
            logger.warning(
                "KEBOOLA_STACK_URL is set but connections are managed in the "
                "registry (/admin/connections); the env value is ignored."
            )
    elif stack_url:
        cfg = validate_connection_config("keboola", {"stack_url": stack_url})
        repo.create(
            id=str(uuid.uuid4()),
            name="keboola",
            source_type="keboola",
            config=cfg,
            token_env="KEBOOLA_STORAGE_TOKEN",
            is_default=True,
            created_by="seed",
        )
        logger.info("Seeded default keboola connection from env/yaml")

    # --- bigquery ---
    project = os.environ.get("BIGQUERY_PROJECT", "") or _yaml_value("data_source", "bigquery", "project")
    existing_bq = repo.list(source_type="bigquery")
    if existing_bq:
        if project and all(r["config"].get("project") != project.strip() for r in existing_bq):
            logger.warning(
                "BIGQUERY_PROJECT is set but connections are managed in the "
                "registry (/admin/connections); the env value is ignored."
            )
    elif project:
        cfg = validate_connection_config(
            "bigquery",
            {
                "project": project,
                "location": os.environ.get("BIGQUERY_LOCATION", "")
                or _yaml_value("data_source", "bigquery", "location")
                or "us",
            },
        )
        billing = _yaml_value("data_source", "bigquery", "billing_project")
        if billing:
            cfg["billing_project"] = billing
        repo.create(
            id=str(uuid.uuid4()),
            name="bigquery",
            source_type="bigquery",
            config=cfg,
            is_default=True,
            created_by="seed",
        )
        logger.info("Seeded default bigquery connection from env/yaml")

    _seed_snowflake(repo)
    _seed_databricks(repo)


def _seed_snowflake(repo) -> None:
    """Seed a default snowflake connection from ``data_source.snowflake.*``.

    Unlike keboola/bigquery, the coordinates here have no separate env-var
    name of their own (``resolve_snowflake_settings`` reads them only via
    ``get_value``, with ``${VAR}``-style interpolation handled by the yaml
    loader itself) — only the two *secret refs* (``token_env``/
    ``private_key_env``) are env-var names, resolved later at query time.
    """
    from src.connection_specs import validate_connection_config

    account = _yaml_value("data_source", "snowflake", "account")
    existing = repo.list(source_type="snowflake")
    if existing:
        if account and all(r["config"].get("account") != account for r in existing):
            logger.warning(
                "data_source.snowflake.account is set but connections are managed in "
                "the registry (/admin/connections); the yaml value is ignored."
            )
        return
    if not account:
        return

    user = _yaml_value("data_source", "snowflake", "user")
    database = _yaml_value("data_source", "snowflake", "database")
    warehouse = _yaml_value("data_source", "snowflake", "warehouse")
    if not (user and database and warehouse):
        # Never usable as-is — resolve_snowflake_settings would still treat
        # this as unconfigured, so seeding it would only create a dead row.
        logger.warning(
            "data_source.snowflake.account is set but user/database/warehouse "
            "are incomplete; skipping snowflake connection seeding"
        )
        return

    auth_type = _yaml_value("data_source", "snowflake", "auth_type") or "password"
    cfg = validate_connection_config(
        "snowflake",
        {
            "account": account,
            "user": user,
            "database": database,
            "warehouse": warehouse,
            "role": _yaml_value("data_source", "snowflake", "role"),
            "auth_type": auth_type,
        },
    )
    if auth_type == "key_pair":
        from connectors.snowflake.settings import SF_PRIVATE_KEY_ENV

        token_env = _yaml_value("data_source", "snowflake", "private_key_env") or SF_PRIVATE_KEY_ENV
    else:
        from connectors.snowflake.settings import SF_TOKEN_ENV

        token_env = _yaml_value("data_source", "snowflake", "token_env") or SF_TOKEN_ENV

    repo.create(
        id=str(uuid.uuid4()),
        name="snowflake",
        source_type="snowflake",
        config=cfg,
        token_env=token_env,
        is_default=True,
        created_by="seed",
    )
    logger.info("Seeded default snowflake connection from yaml")


def _seed_databricks(repo) -> None:
    """Seed a default databricks connection from ``data_source.databricks.*``
    (same no-separate-env-var shape as snowflake — see :func:`_seed_snowflake`)."""
    from src.connection_specs import validate_connection_config

    host = _yaml_value("data_source", "databricks", "host")
    existing = repo.list(source_type="databricks")
    if existing:
        if host and all(r["config"].get("host") != host.rstrip("/") for r in existing):
            logger.warning(
                "data_source.databricks.host is set but connections are managed in "
                "the registry (/admin/connections); the yaml value is ignored."
            )
        return
    if not host:
        return

    warehouse_id = _yaml_value("data_source", "databricks", "warehouse_id")
    if not warehouse_id:
        logger.warning(
            "data_source.databricks.host is set but warehouse_id is missing; skipping databricks connection seeding"
        )
        return

    cfg = validate_connection_config(
        "databricks",
        {
            "host": host,
            "warehouse_id": warehouse_id,
            "catalog": _yaml_value("data_source", "databricks", "catalog"),
        },
    )
    token_env = _yaml_value("data_source", "databricks", "token_env") or "DATABRICKS_TOKEN"
    repo.create(
        id=str(uuid.uuid4()),
        name="databricks",
        source_type="databricks",
        config=cfg,
        token_env=token_env,
        is_default=True,
        created_by="seed",
    )
    logger.info("Seeded default databricks connection from yaml")
