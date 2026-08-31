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

import importlib
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


class AdapterUnavailable(UnknownAdapter):
    """The adapter is registered, but its module could not be imported.

    A subclass of :class:`UnknownAdapter` on purpose — every caller already
    handles "this name will not run" — but with a message that says which of
    the two happened, because "you typed a name nobody registered" and "the
    connector this adapter reads is not installed on this instance" need
    different fixes.
    """


#: Adapters registered at runtime — the built-ins, once imported, plus
#: anything a test or a plugin injects through :func:`register_adapter`. An
#: entry here always wins over the built-in table below.
_REGISTRY: Dict[str, SemanticAdapter] = {}

#: The built-in adapters as DATA — ``name -> (module, class)`` — imported the
#: first time one is actually asked for.
#:
#: Deliberately not four module-level imports. This module is the answer to
#: "which adapters exist", a question the CLI asks to build one line of
#: ``--help``; importing it used to drag in the whole connector chain, and
#: with it ``requests``, which is NOT a core dependency. The result was that
#: `agnes` — every command, including ones that never touch the semantic
#: layer — failed at import on an analyst install without the ``[server]``
#: extra. Names are cheap and implementations are not, so only the names load
#: eagerly.
_BUILTIN_ADAPTERS: Dict[str, tuple[str, str]] = {
    "native": ("src.semantic.adapters.native", "NativeAdapter"),
    "keboola_metastore": ("connectors.keboola.semantic_ossie", "KeboolaMetastoreAdapter"),
    "snowflake_semantic": ("connectors.snowflake.semantic_ossie", "SnowflakeSemanticAdapter"),
    "databricks_metric_views": ("connectors.databricks.semantic_ossie", "DatabricksMetricViewAdapter"),
}


def register_adapter(name: str, adapter: SemanticAdapter) -> None:
    _REGISTRY[name] = adapter


def adapter_names() -> List[str]:
    """Every adapter name, sorted — the single source of truth for anything
    that has to *tell a human* which adapters exist (CLI ``--help``, error
    messages, generated docs). A hand-maintained copy of this list drifts
    silently: the ``agnes admin semantic source add --adapter`` help long
    recommended a ``databricks_semantic`` that was never registered, and every
    reader who copied it got a 400.

    Imports nothing. That is the point — see :data:`_BUILTIN_ADAPTERS`.
    """
    return sorted(set(_REGISTRY) | set(_BUILTIN_ADAPTERS))


def get_adapter(name: str) -> SemanticAdapter:
    """The adapter registered under ``name``, importing it on first use."""
    adapter = _REGISTRY.get(name)
    if adapter is not None:
        return adapter

    target = _BUILTIN_ADAPTERS.get(name)
    if target is None:
        raise UnknownAdapter(f"unknown semantic adapter {name!r}; available: {', '.join(adapter_names())}") from None

    module_path, class_name = target
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        # "Registered but not installed here" — a real state on an analyst
        # install (no `[server]` extra) and worth its own message, rather
        # than a bare ImportError escaping into an endpoint or the sweep.
        raise AdapterUnavailable(
            f"semantic adapter {name!r} is registered but its module ({module_path}) could not be imported: {exc}"
        ) from None

    _REGISTRY[name] = getattr(module, class_name)()
    return _REGISTRY[name]


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
