"""Adapter registry.

An adapter's entire job is to return Ossie documents. It never writes to
semantic_models, metric_definitions, glossary_terms or column_metadata —
validation and persistence happen once, centrally, in the importer. That is
what makes a new source format additive: one function, no new write path.

An adapter must return documents as TEXT, byte-identical to what it received
or composed. It must never parse-and-re-serialize a document: round-tripping
through a YAML dumper would reorder keys and strip comments, and the export
endpoint hands this text straight back out.

``extract`` is the whole required contract. An adapter backed by a CONNECTOR
may additionally implement the optional ``unconfigured_reason`` pre-flight
hook (see :func:`unconfigured_reason`), which is what lets the scheduled
sweep skip a source whose connector is no longer configured on this instance
instead of failing it on every run forever.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class UnknownAdapter(LookupError):
    pass


@runtime_checkable
class SemanticAdapter(Protocol):
    def extract(self, config: dict) -> List[str]:
        """Return Ossie documents as text, exactly as they should be stored."""


@runtime_checkable
class PreflightableAdapter(Protocol):
    """The optional second half of the adapter contract: "could I run at all
    right now?", asked before a scheduled import rather than discovered as a
    failure inside one."""

    def unconfigured_reason(self, config: dict) -> Optional[str]:
        """Why this adapter cannot reach its upstream because the connector
        behind it is not configured on this instance — or ``None`` when it
        is. Never a reason the IMPORT should discover (an unreachable host, a
        rejected credential): those are failures, and failures belong on the
        row."""


_REGISTRY: Dict[str, SemanticAdapter] = {}


def register_adapter(name: str, adapter: SemanticAdapter) -> None:
    _REGISTRY[name] = adapter


def get_adapter(name: str) -> SemanticAdapter:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownAdapter(f"unknown semantic adapter {name!r}; available: {', '.join(sorted(_REGISTRY))}") from None


def unconfigured_reason(source: Dict[str, Any]) -> Optional[str]:
    """Why this source cannot be imported because the connector it reads is
    not configured on this instance — or ``None`` to go ahead.

    The generic half of the check: the sweep asks the SOURCE, the source's
    adapter answers. An adapter that does not implement
    ``unconfigured_reason`` is always ready, so a git/upload source (no
    connector behind it) needs no opinion here and a new connector adapter
    opts in with one method.

    Best-effort by construction, exactly like
    ``legacy_migration.duplicate_upstream_reason``: an unregistered adapter
    or a hook that raises answers ``None`` and the source goes on to its own
    import, which fails there and records the error on the row. A pre-check
    must never be the thing that fails a source.
    """
    name = (source.get("adapter") or "native").strip() or "native"
    try:
        adapter = get_adapter(name)
    except UnknownAdapter:
        return None
    if not isinstance(adapter, PreflightableAdapter):
        return None
    try:
        return adapter.unconfigured_reason(source.get("config") or {})
    except Exception as exc:  # noqa: BLE001 - a pre-check must never be the failure
        logger.info(
            "semantic adapter %s: configuration pre-check raised (%s); importing the source normally",
            name,
            exc,
        )
        return None


from src.semantic.adapters.native import NativeAdapter  # noqa: E402

register_adapter("native", NativeAdapter())

from connectors.keboola.semantic_ossie import KeboolaMetastoreAdapter  # noqa: E402

register_adapter("keboola_metastore", KeboolaMetastoreAdapter())

from connectors.snowflake.semantic_ossie import SnowflakeSemanticAdapter  # noqa: E402

register_adapter("snowflake_semantic", SnowflakeSemanticAdapter())

from connectors.databricks.semantic_ossie import DatabricksMetricViewAdapter  # noqa: E402

register_adapter("databricks_metric_views", DatabricksMetricViewAdapter())
