"""Which ``data_source.<source>`` leaves decide *where* a registration points.

A registered table stores its upstream coordinates relative to the instance's
one connection per source: a Snowflake row keeps ``bucket='GOLD'`` and resolves
the *database* from ``data_source.snowflake.database`` at extract-build time,
baking the result into ``_remote_attach.url`` and into the remote view's
``sf."GOLD"."T"``. Repoint that connection and every existing row of the source
keeps naming a database/schema pair the new upstream does not have: a
``query_mode='remote'`` read fails at bind time (``schema "GOLD" does not
exist``), a materialized sync fails at COPY time, and ``last_sync_status`` goes
on reporting the last *successful* run — so the instance looks healthy while
its data is stale.

The leaves listed here are the ones that change *which upstream* answers, as
opposed to the tuning knobs that live in the same block (scan caps, timeouts,
pool sizes) and break nothing. Changing a credential-pointer leaf
(``token_env``, ``private_key_env``) counts: it swaps which secret is
presented, and therefore which grants apply.

Adding a connector that reads ``data_source.<name>.*``? Add its identity leaves
here — ``tests/test_admin_server_config_connection_guard.py`` fails otherwise.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Per source: the leaves under ``data_source.<source>`` that identify the
# upstream. Everything not listed is treated as tuning and saves unguarded.
CONNECTION_IDENTITY_LEAVES: Dict[str, frozenset[str]] = {
    "snowflake": frozenset(
        {
            "account",
            "user",
            "database",
            "warehouse",
            "role",
            "auth_type",
            "token_env",
            "private_key_env",
        }
    ),
    # `location` is deliberately absent. It is not a coordinate a registration
    # resolves against — the project/dataset in the row still name the same
    # thing — and a wrong region fails loudly at query time with BigQuery's own
    # location-mismatch error rather than silently serving stale data, which is
    # the failure this guard exists for. Keeping it here also produced a
    # standing false positive: the setup form pre-fills `us`, so an instance
    # that never set a region was told every BigQuery table would break on a
    # save that changed nothing about which project it talks to.
    "bigquery": frozenset({"project", "billing_project"}),
    "databricks": frozenset({"host", "warehouse_id", "catalog", "token_env"}),
    "keboola": frozenset({"stack_url", "token_env"}),
}


# What a leaf resolves to when the config does not set it. Taken from the code
# that READS each leaf, not invented here — `resolve_snowflake_settings` falls
# back to `SNOWFLAKE_PASSWORD`, the Keboola client to `KEBOOLA_STORAGE_TOKEN`,
# and so on. Any leaf without an entry defaults to "" (unset), which is what
# every remaining reader coerces a missing value to.
#
# This matters because the callers synthesize full blocks: the setup wizard
# always writes `token_env: "KEBOOLA_STORAGE_TOKEN"` and the Add-data wizard
# always writes `auth_type`. Comparing raw values would score those as
# `None -> "KEBOOLA_STORAGE_TOKEN"` on an instance that simply relied on the
# default — a phantom repoint on a no-op re-save, and (since `token_env` matches
# the audit log's secret-key rule) one reported as `*** -> ***`, which explains
# nothing to the operator it just blocked.
CONNECTION_IDENTITY_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "snowflake": {
        "auth_type": "password",
        "token_env": "SNOWFLAKE_PASSWORD",
        "private_key_env": "SNOWFLAKE_PRIVATE_KEY",
    },
    "keboola": {"token_env": "KEBOOLA_STORAGE_TOKEN"},
    "databricks": {"token_env": "DATABRICKS_TOKEN"},
    "bigquery": {},
}


def _effective(source: str, field: str, block: Dict[str, Any]) -> Any:
    """The value a reader would see for ``field`` — the configured one, or the
    default when it is missing or blank.

    Blank collapses to the default deliberately: every reader resolves it as
    ``get_value(...) or DEFAULT``, so an empty string and an absent key mean the
    same thing to the running system, and a guard that disagreed with the
    readers would fire on a difference that does not exist.
    """
    value = block.get(field)
    if value is None or value == "":
        return CONNECTION_IDENTITY_DEFAULTS.get(source, {}).get(field, "")
    return value


def identity_changes(
    source: str,
    before: Dict[str, Any] | None,
    after: Dict[str, Any] | None,
) -> List[Dict[str, Any]]:
    """Return the identity leaves whose value differs between two config blocks.

    Each entry is ``{"field", "before", "after"}``, ordered by field name so the
    refusal message is stable, and carries the *effective* values (defaults
    resolved) rather than the raw ones — the operator is being asked about what
    the instance will actually connect to.

    A leaf that is genuinely unset before and set after counts as a change:
    going from "no role" to a role decides which grants apply, exactly the class
    of edit this guard exists to surface.
    """
    leaves = CONNECTION_IDENTITY_LEAVES.get(source)
    if not leaves:
        return []

    before = before or {}
    after = after or {}
    changes: List[Dict[str, Any]] = []
    for field in sorted(leaves):
        if field not in after:
            # Absent from the patch-merged block means the operator did not
            # touch it, so there is nothing to warn about.
            continue
        old = _effective(source, field, before)
        new = _effective(source, field, after)
        if old != new:
            changes.append({"field": field, "before": old, "after": new})
    return changes
