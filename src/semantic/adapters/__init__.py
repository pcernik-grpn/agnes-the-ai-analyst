"""Adapter registry.

An adapter's entire job is to return Ossie documents. It never writes to
semantic_models, metric_definitions, glossary_terms or column_metadata —
validation and persistence happen once, centrally, in the importer. That is
what makes a new source format additive: one function, no new write path.

An adapter must return documents as TEXT, byte-identical to what it received
or composed. It must never parse-and-re-serialize a document: round-tripping
through a YAML dumper would reorder keys and strip comments, and the export
endpoint hands this text straight back out.
"""

from __future__ import annotations

from typing import Dict, List, Protocol, runtime_checkable


class UnknownAdapter(LookupError):
    pass


@runtime_checkable
class SemanticAdapter(Protocol):
    def extract(self, config: dict) -> List[str]:
        """Return Ossie documents as text, exactly as they should be stored."""


_REGISTRY: Dict[str, SemanticAdapter] = {}


def register_adapter(name: str, adapter: SemanticAdapter) -> None:
    _REGISTRY[name] = adapter


def get_adapter(name: str) -> SemanticAdapter:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownAdapter(f"unknown semantic adapter {name!r}; available: {', '.join(sorted(_REGISTRY))}") from None


from src.semantic.adapters.native import NativeAdapter  # noqa: E402

register_adapter("native", NativeAdapter())

from connectors.keboola.semantic_ossie import KeboolaMetastoreAdapter  # noqa: E402

register_adapter("keboola_metastore", KeboolaMetastoreAdapter())

from connectors.snowflake.semantic_ossie import SnowflakeSemanticAdapter  # noqa: E402

register_adapter("snowflake_semantic", SnowflakeSemanticAdapter())

from connectors.databricks.semantic_ossie import DatabricksMetricViewAdapter  # noqa: E402

register_adapter("databricks_metric_views", DatabricksMetricViewAdapter())
