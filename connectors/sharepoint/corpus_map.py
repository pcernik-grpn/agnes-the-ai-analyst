"""Producer corpus-map construction — ONE implementation for both handoffs.

The external extraction producer routes each crawled row to a collection
with its ``corpusmap.corpus_for()`` resolver, whose keys are matched
against the crawler's OWN row shape:

    "<site display name>"                   every row of the site
    "<site display name>/<folder path>"     rows whose drive-relative path
                                            sits under the folder prefix

Two facts make the wizard's stored ``display_path`` unusable verbatim:

1. Crawler row paths are DRIVE-relative (``parentReference.path`` with the
   ``/drives/<id>/root:`` prefix stripped), so they never contain the
   document-library segment — while the wizard breadcrumb for a folder
   scope always does ("Site/Documents/Folder"). Handing that through would
   match nothing and every row would be silently unmapped.
2. The resolver matches by path COMPONENTS, so UI-authored separators like
   "Site / Documents" (spaces around the slash) poison matching unless
   segments are stripped.

This module owns the translation. Both the ``corpus-extraction`` job
handler (``AGNES_EXTRACTION_CORPUS_MAP`` child-env handoff,
``app/worker/kinds.py``) and the pull handoff (``GET
/api/admin/sharepoint/connections/{id}/corpus-map``) go through it, so the
two surfaces cannot drift.

Scope KIND is decided structurally from the Graph id shape — a composite
site id contains ``,`` (``<host>,<siteGuid>,<webGuid>``), a drive id starts
with ``b!``, anything else is a folder/item id. No Graph round-trip: the
translation must work for scope rows confirmed before this module existed.

**Permission zones (2026-08-31 plan, Task 7).** An ACTIVE zone
(``connectors.sharepoint.acl_sync.active_zone_rows`` — a broken-inheritance
subtree promoted to its own collection, see that module) becomes an
ADDITIONAL, NESTED map key: its ``zone_item_id`` is always a plain Graph
folder item id (no ``,``, no ``b!``), so it always takes the folder branch
above regardless of its parent scope's own kind, and its stored
``display_path`` is always the parent scope's own ``display_path`` extended
by the zone's folder path — so the resulting key is always a strict
extension of the parent scope's own key (e.g. parent ``"Site/Team"``, zone
``"Site/Team/Legal"``).

This means the PRODUCER'S RESOLVER MUST match a document's path against
these keys longest-prefix-first — a resolver that matches shortest-prefix
(or picks arbitrarily among several matching keys) would route zone content
to the wider, less-restricted parent collection instead of the zone's own.
That ordering is NOT enforced or verified here; a producer that gets it
wrong fails closed anyway, because the server-side ingest gate (2026-08-31
plan, Task 5) refuses any document whose source path falls under an active
zone but was uploaded to a DIFFERENT collection than that zone's own — this
module's contract is "give the producer an unambiguous, correctly-nested
map", not "guarantee the producer reads it correctly".

A dissolved zone (``status != "active"``) is never mapped — its content is
re-homed to the parent scope, so its subtree must route there too.
"""

from __future__ import annotations

import logging
from typing import Sequence

logger = logging.getLogger(__name__)


class CorpusMapError(ValueError):
    """A confirmed-scope set that cannot be turned into an unambiguous
    producer corpus map. Always a configuration problem an admin must fix —
    callers surface the message verbatim (job failure / 409), never route
    rows on a best guess."""


def _map_key(source_scope_id: str, display_path: str) -> str:
    """One scope row -> its ``corpus_for()`` key (see module docstring)."""
    segments = [part.strip() for part in display_path.split("/") if part.strip()]
    if "," in source_scope_id:
        # Site scope. A site display name may itself contain "/" — the
        # resolver's own contract says such a site can only use the
        # bare-site form, so the whole (trimmed) name is the key either way.
        return display_path.strip()
    if source_scope_id.startswith("b!"):
        # Drive (document-library) scope. The producer's map format has no
        # drive dimension, so drive granularity degrades to site-wide
        # routing — worth a warning because on a site with several
        # libraries this key also catches the other libraries' rows.
        logger.warning(
            "sharepoint corpus map: drive scope %r (%s) degrades to site-wide key %r — "
            "the producer map format has no drive dimension",
            display_path,
            source_scope_id,
            segments[0] if segments else display_path.strip(),
        )
        return segments[0] if segments else display_path.strip()
    # Folder scope: the wizard breadcrumb is "Site/<library>/<folders...>";
    # crawler paths are drive-relative, so the library segment is dropped.
    if len(segments) >= 3:
        return "/".join([segments[0], *segments[2:]])
    return "/".join(segments)


def _add_row(
    row_id: object,
    display_path: object,
    collection_id: object,
    out: dict[str, str],
    owners: dict[str, str],
    *,
    what: str,
) -> None:
    """Shared per-row logic for both the scope loop and the zone loop of
    :func:`producer_corpus_map` — one ``_map_key`` call, one ambiguity
    check, so the two loops cannot silently diverge on what "unambiguous"
    means. ``what`` (``"scope"``/``"zone"``) only shapes error text."""
    if not row_id:
        return
    if not collection_id or not display_path:
        raise CorpusMapError(
            f"{what} {row_id!r} has no "
            f"{'collection_id' if not collection_id else 'display_path'} — "
            f"cannot build a corpus map key for it"
        )
    key = _map_key(str(row_id), str(display_path))
    if not key:
        raise CorpusMapError(f"{what} {row_id!r} produced an empty corpus map key from display_path {display_path!r}")
    if key in out and out[key] != str(collection_id):
        raise CorpusMapError(
            f"corpus map key {key!r} is claimed by two rows with different "
            f"collections ({owners[key]!r} and {row_id!r}) — remove or "
            "narrow one of the overlapping scopes/zones"
        )
    out[key] = str(collection_id)
    owners[key] = str(row_id)


def producer_corpus_map(scopes: list, zones: Sequence[dict] = ()) -> dict[str, str]:
    """``{corpus_for-key: collection_id}`` for every confirmed scope row,
    PLUS one additional nested key per ACTIVE permission zone (2026-08-31
    plan, Task 7 — see the module docstring's "Permission zones" section for
    why the producer's own resolver must match these longest-prefix-first).
    ``zones`` is normally ``connectors.sharepoint.acl_sync.active_zone_rows
    (connection)`` — a caller may also pass the connection's full,
    unfiltered ``zone_rows(connection)``; a row with ``status != "active"``
    (a dissolved zone) is filtered out here regardless, never mapped.

    Raises :class:`CorpusMapError` on a row that cannot produce a key
    (missing ``display_path``/``collection_id``) and on two rows (scope or
    zone, in any combination) whose keys collide with DIFFERENT collections
    (e.g. a site scope plus a drive scope of the same site) — silently
    routing every row to whichever one won the dict insert is the
    silent-loss class this exists to prevent.
    """
    out: dict[str, str] = {}
    owners: dict[str, str] = {}
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        _add_row(
            scope.get("source_scope_id"),
            scope.get("display_path"),
            scope.get("collection_id"),
            out,
            owners,
            what="scope",
        )
    for zone in zones:
        if not isinstance(zone, dict) or zone.get("status") != "active":
            continue
        _add_row(
            zone.get("zone_item_id"),
            zone.get("display_path"),
            zone.get("collection_id"),
            out,
            owners,
            what="zone",
        )
    return out
