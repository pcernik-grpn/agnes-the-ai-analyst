"""SharePoint source-ACL ingest gate (2026-08-31 plan, Task 5).

**Trust boundary.** The subtree sweep (:mod:`connectors.sharepoint.acl_sync`)
writes an exclusion list and a permission-zone map into each connection's own
config, then hands both to the external crawl producer via an env var / the
producer corpus map (``app/worker/kinds.py``). Honoring that list on the
producer's own crawl side is advisory — Agnes cannot verify a third-party
process actually skipped what it was told to skip, and the producer's
longest-prefix zone routing is not guaranteed either (see the parent plan's
"Known behavioral facts"). This module is the enforcement half: it re-derives
the same exclusion/zone state from the connection's config on every call and
refuses, server-side, any upload or fact-ingest batch that would otherwise
land content Agnes itself knows should never have been crawled — MUST NOT,
not "should not" (spec §1.2's ratified fork).

Two call sites consume this module: the collection upload endpoint
(``app/api/collections.py::upload_files``, before a single byte is stored)
and the fact-graph ingest endpoint (``app/api/facts.py::facts_ingest``,
before ``facts_repo().ingest_batch``).

Matching semantics (locked, do not relax):

* stable-id matching is EXACT — ``"graph:<item-id>"`` against a ``kind==
  "file"`` exclusion entry's ``item_id``;
* path matching is component-safe prefix — ``path == p or path.startswith(p
  + "/")`` — against a ``kind=="folder"`` exclusion entry's ``rel_path`` and
  an ACTIVE permission zone's ``rel_path``;
* a legacy exclusion entry (pre-2026-08-31-plan, no ``kind``/``rel_path``)
  cannot be path-matched — its subtree was never crawled with rel-path
  tracking, so nothing arrives under that path anyway — and is not
  stable-id-matched either (only ``kind=="file"`` entries carry one).

The gate is a strict no-op — :func:`source_acl_index_for_collection` returns
``None`` — unless BOTH ``acl_mirroring.enabled`` is on AND the collection is
actually one of a SharePoint connection's mirrored-scope or active-zone
collections. Pure, uncached functions: both call sites are request-scoped and
a config read is one repo call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from connectors.sharepoint.acl_sync import active_zone_rows
from src.repositories import source_connections_repo


@dataclass(frozen=True)
class SourceAclIndex:
    """Everything :func:`source_acl_refusal` needs to judge one collection's
    uploads — pre-computed once per call from a connection's config."""

    excluded_stable_ids: "frozenset[str]"
    excluded_rel_prefixes: "tuple[str, ...]"
    foreign_zone_prefixes: "tuple[str, ...]"


def _nested_under(rel_path: str, root: str) -> bool:
    """Component-safe prefix test — ``rel_path`` sits at or under ``root``."""
    return bool(rel_path) and (rel_path == root or rel_path.startswith(root + "/"))


def _scope_exclusions(scope: Dict[str, Any], *, under: Optional[str] = None) -> "tuple[frozenset, tuple]":
    """Split one scope's ``excluded_subtrees`` into stable ids (``kind==
    "file"``) and rel-path prefixes (``kind=="folder"``, non-empty
    ``rel_path`` only). ``under`` restricts to entries nested at or below
    that rel-path prefix (used to scope a permission zone's own index to its
    own deeper exclusions) — ``None`` takes every entry, unfiltered."""
    stable_ids: set = set()
    rel_prefixes: List[str] = []
    for entry in scope.get("excluded_subtrees") or []:
        if not isinstance(entry, dict):
            continue
        rel_path = entry.get("rel_path")
        if under is not None and not _nested_under(rel_path or "", under):
            continue
        item_id = entry.get("item_id")
        kind = entry.get("kind")
        if kind == "file" and item_id:
            stable_ids.add(f"graph:{item_id}")
        elif kind == "folder" and rel_path:
            rel_prefixes.append(rel_path)
        # A legacy entry (no `kind`) matches neither branch — inert by
        # design, per the module docstring's matching semantics.
    return frozenset(stable_ids), tuple(rel_prefixes)


def source_acl_index_for_collection(collection_id: str) -> Optional[SourceAclIndex]:
    """Build the ACL index for ``collection_id``, or ``None`` when the gate
    does not apply — ``acl_mirroring`` is off, or the collection is not one
    of a SharePoint connection's mirrored-scope or ACTIVE-zone collections
    (a plain admin-created collection, a manual-mode scope, or a DISSOLVED
    zone's collection — the sweep's own retroactive cleanup owns that case,
    not this gate)."""
    from app.instance_config import feature_enabled

    if not feature_enabled("acl_mirroring", "enabled", env_var="AGNES_ACL_MIRRORING_ENABLED", default=False):
        return None

    for connection in source_connections_repo().list(source_type="sharepoint"):
        config = connection.get("config") or {}
        scopes = [s for s in (config.get("scopes") or []) if isinstance(s, dict)]
        zones = active_zone_rows(connection)

        for scope in scopes:
            if scope.get("collection_id") != collection_id:
                continue
            stable_ids, rel_prefixes = _scope_exclusions(scope)
            scope_id = scope.get("source_scope_id")
            foreign_zone_prefixes = tuple(
                z["rel_path"] for z in zones if z.get("parent_scope_id") == scope_id and z.get("rel_path")
            )
            return SourceAclIndex(
                excluded_stable_ids=stable_ids,
                excluded_rel_prefixes=rel_prefixes,
                foreign_zone_prefixes=foreign_zone_prefixes,
            )

        for zone in zones:
            if zone.get("collection_id") != collection_id:
                continue
            zone_rel = zone.get("rel_path") or ""
            parent_scope_id = zone.get("parent_scope_id")
            parent_scope = next((s for s in scopes if s.get("source_scope_id") == parent_scope_id), None)

            if parent_scope is not None and zone_rel:
                stable_ids, rel_prefixes = _scope_exclusions(parent_scope, under=zone_rel)
            else:
                stable_ids, rel_prefixes = frozenset(), tuple()

            foreign_zone_prefixes = tuple(
                other["rel_path"]
                for other in zones
                if other.get("parent_scope_id") == parent_scope_id
                and other.get("collection_id") != collection_id
                and other.get("rel_path")
                and zone_rel
                and _nested_under(other["rel_path"], zone_rel)
            )
            return SourceAclIndex(
                excluded_stable_ids=stable_ids,
                excluded_rel_prefixes=rel_prefixes,
                foreign_zone_prefixes=foreign_zone_prefixes,
            )

    return None


def _prefix_match(path: str, prefixes: "tuple[str, ...]") -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def source_acl_refusal(index: SourceAclIndex, *, path: Optional[str], stable_id: Optional[str]) -> Optional[str]:
    """Judge one file/document against a pre-built index. Returns
    ``"source_acl_excluded"`` (an excluded folder subtree or file),
    ``"source_acl_zone_mismatch"`` (content that belongs under another
    collection's active permission zone), or ``None`` (no refusal — includes
    every input with neither a usable ``path`` nor a usable ``stable_id``,
    same as an index with no exclusions at all)."""
    if stable_id and stable_id in index.excluded_stable_ids:
        return "source_acl_excluded"
    if path:
        if _prefix_match(path, index.excluded_rel_prefixes):
            return "source_acl_excluded"
        if _prefix_match(path, index.foreign_zone_prefixes):
            return "source_acl_zone_mismatch"
    return None
