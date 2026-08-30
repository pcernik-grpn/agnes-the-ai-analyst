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
"""

from __future__ import annotations

import logging

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


def producer_corpus_map(scopes: list) -> dict[str, str]:
    """``{corpus_for-key: collection_id}`` for every confirmed scope row.

    Raises :class:`CorpusMapError` on a row that cannot produce a key
    (missing ``display_path``/``collection_id``) and on two scopes whose
    keys collide with DIFFERENT collections (e.g. a site scope plus a drive
    scope of the same site) — silently routing every row to whichever scope
    won the dict insert is the silent-loss class this exists to prevent.
    """
    out: dict[str, str] = {}
    owners: dict[str, str] = {}
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        source_scope_id = scope.get("source_scope_id")
        collection_id = scope.get("collection_id")
        display_path = scope.get("display_path")
        if not source_scope_id:
            continue
        if not collection_id or not display_path:
            raise CorpusMapError(
                f"scope {source_scope_id!r} has no "
                f"{'collection_id' if not collection_id else 'display_path'} — "
                "cannot build a corpus map key for it; re-confirm the scope in the wizard"
            )
        key = _map_key(str(source_scope_id), str(display_path))
        if not key:
            raise CorpusMapError(
                f"scope {source_scope_id!r} produced an empty corpus map key from display_path {display_path!r}"
            )
        if key in out and out[key] != str(collection_id):
            raise CorpusMapError(
                f"corpus map key {key!r} is claimed by two scopes with different "
                f"collections ({owners[key]!r} and {source_scope_id!r}) — remove or "
                "narrow one of the overlapping scopes"
            )
        out[key] = str(collection_id)
        owners[key] = str(source_scope_id)
    return out
