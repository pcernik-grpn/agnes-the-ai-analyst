"""Slice 4a's single read surface for per-scope audience-class mappings
(2026-08-30 plan, Task 8; spec 2026-08-28-sharepoint-acl-mirroring-design.md
§4.1-4.3).

An audience class (e.g. ``"full"``, ``"redacted"``) is configured per
SharePoint scope at wizard time as an ORDERED mapping ``{class_name ->
group_ids}`` — most-privileged class first, the order Task 10's projection
dedup and Task 11's "top class only" rule both rank by
(``app/api/admin_sharepoint.py``'s ``ConfirmScopeBody.audience_classes`` /
``_scope_out``). This module is the ONLY place that scans ``source_connections``
rows' ``config.scopes`` to read that mapping back at runtime — Tasks 9-11
(caller audience resolution, the ``facts_pg`` visibility predicate, and the
collections document-text gate) must call :func:`audience_class_map` /
:func:`tiered_collection_ids` rather than re-scanning scopes themselves, so
the on-disk shape has exactly one reader.

Plain functions, no caching layer beyond what the underlying config read
already does — call sites are repo-level and infrequent (once per request at
most, per the plan), not a hot loop.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List, Tuple

from src.repositories import source_connections_repo


def audience_class_map() -> Dict[str, List[Tuple[str, FrozenSet[str]]]]:
    """``{collection_id: [(class_name, frozenset(group_ids)), ...]}``, each
    collection's classes in the wizard-persisted privilege order (most-
    privileged first). Built by scanning every ``source_type='sharepoint'``
    connection's ``config.scopes`` — a scope with no ``audience_classes`` (or
    an empty list) contributes no entry, so a plain (non-tiered) collection
    is simply absent from the result rather than mapped to ``[]``.
    """
    result: Dict[str, List[Tuple[str, FrozenSet[str]]]] = {}
    for connection in source_connections_repo().list(source_type="sharepoint"):
        scopes = (connection.get("config") or {}).get("scopes")
        if not isinstance(scopes, list):
            continue
        for scope in scopes:
            if not isinstance(scope, dict):
                continue
            collection_id = scope.get("collection_id")
            classes = scope.get("audience_classes")
            if not collection_id or not isinstance(classes, list) or not classes:
                continue
            ordered = [
                (cls["name"], frozenset(cls.get("group_ids") or []))
                for cls in classes
                if isinstance(cls, dict) and cls.get("name")
            ]
            if ordered:
                result[collection_id] = ordered
    return result


def tiered_collection_ids() -> FrozenSet[str]:
    """The set of collection ids carrying a non-empty ``audience_classes``
    mapping — i.e. ``tiered`` per ``app/api/admin_sharepoint.py::_scope_out``.
    Convenience for Tasks 10/11 so neither re-derives it from
    :func:`audience_class_map` by hand."""
    return frozenset(audience_class_map().keys())
