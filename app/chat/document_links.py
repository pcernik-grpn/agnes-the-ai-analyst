"""Resolving a `document:` source claim's ref to a real, RBAC-safe file link.

`app/chat/sources.py` renders a `document:` claim as a plain label, never a
link — its docstring on `_document_needles` explains why: the ref is a
filename (or occasionally a fact-graph subject id) as the model wrote it, not
a stable id like a `table:`'s registry id or a `metric:`'s `family/name`.
Document detail lives at `/library/{slug}/f/{file_id}`, and nothing in the
claim names either half of that path.

This module is the "own change" that comment deferred: given a claim's ref,
find the ONE corpus file it names, scoped to collections the caller can
actually reach (`app.auth.access.accessible_collection_ids` — the same
fail-closed set `/api/collections` and `/library` already use), and report
back `/library/{slug}/f/{file_id}` for a chip to link to.

Deliberately conservative. `CorpusFilesRepository.match_filenames` runs a
substring LIKE scan for candidates — necessary because a citation ref may be
peeled (a directory prefix, an extension, a model's own description tail; see
`_document_needles`) and a stored filename may itself be rewritten (spaces to
underscores, a timestamp before the extension; see `sources._normalize`'s
docstring) — but a substring hit is a CANDIDATE, not a citation. Resolution
re-normalizes every candidate the same way `verify()` does and requires
EXACTLY one distinct file id to survive; zero or more than one both resolve
to `None`. A wrong link would be worse than no link — the chip is now the
one thing on the row a reader can click without checking anything first.
"""

from __future__ import annotations

from typing import Any

from app.chat.sources import SourceClaim, _document_needles, _normalize


def _candidate_names(row: dict[str, Any]) -> set[str]:
    """Every spelling of a stored file's name worth comparing against a
    citation's needles — mirrors the peeling `_document_needles` does on the
    OTHER side of the match: basename and extensionless stem, of both the
    stored `filename` and the last path segment of `path` (a SharePoint-style
    corpus stores the citable name in `path`, a plain upload in `filename`)."""
    names: set[str] = set()
    for raw in (row.get("filename"), row.get("path")):
        if not raw:
            continue
        base = raw.rsplit("/", 1)[-1]
        names.add(base)
        names.add(base.rsplit(".", 1)[0])
    return {n for n in names if n}


def resolve_document_url(ref: str, user: Any) -> str | None:
    """`/library/{slug}/f/{file_id}` for the one accessible file ``ref``
    names, or ``None`` when nothing resolves unambiguously.

    ``user`` is whatever `app.auth.access.accessible_collection_ids` accepts
    (a plain user dict, at minimum carrying ``id`` — a restricted
    ``SessionPrincipal``/``AgentPrincipal`` works too, since that function
    already handles both). ``None``/falsy ``ref`` short-circuits.
    """
    if not ref:
        return None

    from app.auth.access import PRINCIPAL_TYPES, accessible_collection_ids
    from app.resource_types import ResourceType
    from src.repositories import corpus_files_repo, file_corpora_repo, resource_grants_repo

    allowed = accessible_collection_ids(user)
    corpus_ids = None if allowed is None else list(allowed)

    # A file shared out of its collection (per-file grant) is reachable at
    # its own `/library/{slug}/f/{file_id}` page — `library_file_detail`'s
    # `can_parent OR file_granted` check — even when the PARENT collection is
    # not itself in `corpus_ids`. Without folding that same grant in here,
    # this resolver's scope would be narrower than the page it links to: a
    # legitimate citation would silently stay unlinked rather than resolve.
    # Skipped for admins (corpus_ids is None already covers everything) and
    # for a restricted Principal (no per-user grant identity to look up —
    # its authority is the collection-level intersection alone, same as
    # `accessible_collection_ids`'s own ownership-union skip).
    extra_file_ids: list[str] | None = None
    if corpus_ids is not None and not isinstance(user, PRINCIPAL_TYPES):
        user_id = user.get("id") if hasattr(user, "get") else None
        if user_id:
            extra_file_ids = resource_grants_repo().list_resource_ids_for_user(user_id, ResourceType.CORPUS_FILE.value)

    if corpus_ids is not None and not corpus_ids and not extra_file_ids:
        return None

    needles = _document_needles(ref.lower())
    if not needles:
        return None

    rows = corpus_files_repo().match_filenames(corpus_ids, needles, extra_file_ids=extra_file_ids, limit=20)
    if not rows:
        return None

    normalized_needles = {_normalize(n) for n in needles}
    matched_ids: set[str] = set()
    matched_row: dict[str, Any] = {}
    for row in rows:
        if any(_normalize(cand) in normalized_needles for cand in _candidate_names(row)):
            matched_ids.add(row["id"])
            matched_row = row

    # Zero matches (the LIKE scan found substrings but none survives
    # re-normalized equality) or more than one (an ambiguous name across
    # accessible collections): both resolve to no link, never a guess.
    if len(matched_ids) != 1:
        return None

    corpus = file_corpora_repo().get(matched_row["corpus_id"])
    if not corpus or not corpus.get("slug"):
        return None
    return f"/library/{corpus['slug']}/f/{matched_row['id']}"


def attach_document_urls(sources: dict[str, Any], user: Any) -> dict[str, Any]:
    """Adds a ``url`` key to every ``document:`` claim in a serialized
    :class:`~app.chat.sources.SourcesVerdict` dict that resolves — the
    server-side half of the chip-link contract, mirroring how `table`/
    `metric`/`glossary` links are already built (client-side, because their
    ref IS a stable id — see ``_claimHref`` in ``chat.js``).

    Mutates and returns ``sources`` in place; a no-op when there is no
    ``user`` to scope resolution to, or no document claim to resolve.
    """
    claims = sources.get("claims") or []
    if not user or not any(c.get("kind") == "document" for c in claims):
        return sources
    for claim in claims:
        if claim.get("kind") != "document":
            continue
        url = resolve_document_url(claim.get("ref", ""), user)
        if url:
            claim["url"] = url
    return sources


__all__ = ["SourceClaim", "attach_document_urls", "resolve_document_url"]
