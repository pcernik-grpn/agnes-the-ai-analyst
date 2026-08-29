"""Bundle (zip) ingestion — unpack an uploaded archive into child corpus files.

The archive row itself never carries chunks; each supported member becomes its
own ``corpus_files`` row (``parent_file_id`` → the archive) stored
content-addressed like a direct upload, then driven through the normal
``ingest_file`` router. The archive row's final status aggregates its
children: ``indexed`` when at least one child indexed, else ``needs_review``.

Safety: member names are validated against zip-slip (absolute paths, ``..``);
nested archives are rejected per-member; member count and total uncompressed
size are capped before any extraction. Metadata junk (``__MACOSX/``,
``.DS_Store``) is skipped silently.

Idempotent re-ingest: children are matched to existing rows by
``(filename, sha256)`` and reused, so per-file idempotency downstream
(chunk replacement, derived-table re-registration) applies; unmatched
leftovers from a previous run are deleted along with their chunks.

**Member anchors (citability follow-up, fact-graph-over-Collections design
§6/§7):** every successfully-stored member also gets its own
``corpus_file_sources`` row (see ``_member_stable_id``), so a fact-graph claim
can cite the exact member that contains the evidence, not just the archive —
and so the member's ``corpus_files.id`` (and therefore its claims) survives a
re-sync exactly like a top-level file's does. Writing the anchor is best-effort:
on a DuckDB-backed instance (``corpus_file_sources`` is PG-only, A3 ratchet)
this silently no-ops, and bundle ingestion itself stays fully backend-agnostic.
"""

from __future__ import annotations

import logging
import posixpath
import zipfile
from collections import defaultdict
from typing import Any, Callable, Optional

from src.corpus_allowlist import MAX_UPLOAD_BYTES, classify
from src.file_storage import store_corpus_bytes
from src.ingest.confluence import normalize_html
from src.ingest.member_identity import is_reserved_member_stable_id
from src.repositories import (
    RequiresPostgresBackend,
    corpus_chunks_repo,
    corpus_file_sources_repo,
    corpus_files_repo,
)

logger = logging.getLogger(__name__)

MAX_BUNDLE_MEMBERS = 1000
MAX_BUNDLE_TOTAL_BYTES = 1024 * 1024 * 1024  # 1 GiB uncompressed

_SKIP_PREFIXES = ("__MACOSX/",)
_SKIP_BASENAMES = {".DS_Store", "Thumbs.db"}


def _is_unsafe(name: str) -> bool:
    """True for member names that could escape the extraction root."""
    if name.startswith(("/", "\\")):
        return True
    norm = posixpath.normpath(name.replace("\\", "/"))
    if norm.startswith("..") or "/../" in f"/{norm}/":
        return True
    # Windows drive letters ("C:\...") in the first segment.
    return ":" in norm.split("/", 1)[0]


def _is_junk(name: str) -> bool:
    return name.startswith(_SKIP_PREFIXES) or posixpath.basename(name) in _SKIP_BASENAMES


def _is_stable_id_race(exc: Exception) -> bool:
    """True when ``exc`` is the ``(corpus_id, source_stable_id)`` unique-
    constraint violation two overlapping ``ingest_bundle`` runs can produce
    (review finding 3) — never any OTHER integrity error, which should still
    surface as a real bug rather than being silently swallowed here."""
    from sqlalchemy.exc import IntegrityError

    return isinstance(exc, IntegrityError) and "uq_corpus_file_sources_corpus_stable_id" in str(exc)


def _member_stable_id(archive_file_id: str, member_name: str) -> str:
    """Deterministic ``corpus_file_sources.source_stable_id`` for one member.

    ``"<archive corpus_files.id>!<member path>"`` — this shape is RESERVED,
    not merely conventionally distinct: a real producer-supplied top-level
    ``source_stable_id`` follows the crawler's own convention
    (``graph:<driveItem-id>``, ``local:<relpath>``, spec §6), which never
    starts with ``cf_``, the fixed prefix every ``corpus_files.id`` carries
    (``src/repositories/corpus_files.py::add``) — but the shape is NOT a
    secret (an ordinary file listing exposes both halves to anyone with
    collection READ access), so ``is_reserved_member_stable_id`` REFUSES a
    caller-supplied value on this shape at every entry point that accepts
    one (the upload endpoint's ``source_stable_ids``, the facts ingest
    ``documents[].stable_id``) — only this function may mint one. The
    assertion below keeps that invariant self-checking rather than merely
    documented.

    The archive's OWN row id is used, never its (optional) own
    ``source_stable_id``: it is always present — every ``corpus_files`` row
    has one, including a bare manual zip upload with no crawler anchor at
    all — and, like a top-level file's id, stays stable across a re-sync as
    long as the archive itself is matched by stable_id/path (§6). That is
    what makes a routine re-upload of an N-member zip keep the N-1 unchanged
    members' anchors (and therefore their claims), not just their rows.
    """
    stable_id = f"{archive_file_id}!{member_name}"
    assert is_reserved_member_stable_id(stable_id), (
        f"minted stable_id {stable_id!r} does not match the reserved shape it is supposed to occupy "
        f"— archive_file_id {archive_file_id!r} must start with 'cf_'"
    )
    return stable_id


def ingest_bundle(
    corpus_id: str,
    file_id: str,
    storage_path: str,
    *,
    ingest_child: Optional[Callable[[str], str]] = None,
) -> str:
    """Unpack the archive at ``storage_path`` and ingest each member.

    Sets the archive row's own ``processing_status`` in every path and
    returns it (``indexed | needs_review | rejected``). ``ingest_child``
    is injectable for tests; production uses ``ingest_file``.
    """
    if ingest_child is None:
        from src.ingest.runner import ingest_file as ingest_child  # circular-safe

    cf_repo = corpus_files_repo()
    try:
        sources_repo = corpus_file_sources_repo()
    except RequiresPostgresBackend:
        # DuckDB-backed instance: bundles still ingest, just unanchored — see
        # the module docstring's "Member anchors" paragraph.
        sources_repo = None

    try:
        zf = zipfile.ZipFile(storage_path)
        infos = [i for i in zf.infolist() if not i.is_dir() and not _is_junk(i.filename)]
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("bundle open failed file_id=%s: %s", file_id, exc)
        cf_repo.set_status(file_id, status="rejected", detail={"reason": "invalid_archive"})
        return "rejected"

    with zf:
        return _ingest_bundle_members(zf, infos, file_id, corpus_id, cf_repo, sources_repo, ingest_child)


def _ingest_bundle_members(
    zf: "zipfile.ZipFile",
    infos: list,
    file_id: str,
    corpus_id: str,
    cf_repo: Any,
    sources_repo: Optional[Any],
    ingest_child: Callable[[str], str],
) -> str:
    if len(infos) > MAX_BUNDLE_MEMBERS:
        cf_repo.set_status(
            file_id,
            status="rejected",
            detail={"reason": "too_many_members", "members": len(infos), "max": MAX_BUNDLE_MEMBERS},
        )
        return "rejected"
    if sum(i.file_size for i in infos) > MAX_BUNDLE_TOTAL_BYTES:
        cf_repo.set_status(
            file_id,
            status="rejected",
            detail={"reason": "bundle_too_large", "max_bytes": MAX_BUNDLE_TOTAL_BYTES},
        )
        return "rejected"

    # Existing children from a previous run, for row reuse. A MULTI-map, not
    # a plain dict: two entries in ONE zip can legally share the same
    # (filename, sha256) — most commonly two rejected members with the same
    # name, since every rejected member is stored with `sha256=""`. A plain
    # `dict` comprehension collapses duplicate keys to whichever prior row
    # sorted last, silently dropping the rest of `prior` — invisible to both
    # the reuse match below AND (before this fix) the prune loop, so an
    # unmatched duplicate orphaned permanently, one more per re-sync round
    # (review finding 4). `_claim_prior` consumes with `pop(0)`, so N
    # same-key prior rows pair up with N same-key current entries in file
    # order; only genuinely-unmatched leftovers (renamed/removed members)
    # remain in `prior_by_key` for the prune step below.
    prior_by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for k in cf_repo.list_children(file_id):
        prior_by_key[(k["filename"], k["sha256"])].append(k)

    def _claim_prior(name: str, sha256: str) -> Optional[dict]:
        bucket = prior_by_key.get((name, sha256))
        return bucket.pop(0) if bucket else None

    # (child_id, member name, member sha256) for every successfully-stored
    # member — anchors are written AFTER the prune step below, not here: a
    # renamed/changed member reuses the SAME `_member_stable_id` (it is keyed
    # on the archive's own, unchanged, id + the member's name) as the row it
    # replaces, so writing its anchor before that OLD row (and its anchor,
    # via FK cascade) is pruned would collide on `(corpus_id,
    # source_stable_id)` — the still-live old anchor is still holding it.
    to_anchor: list[tuple[str, str, str]] = []
    counts: dict[str, int] = {"indexed": 0, "rejected": 0, "needs_review": 0, "pending": 0, "processing": 0}
    children = 0

    def _add_rejected(name: str, size: int, reason: str) -> None:
        existing = _claim_prior(name, "")
        if existing:
            cid = existing["id"]
        else:
            cid = cf_repo.add(
                corpus_id=corpus_id,
                filename=name,
                sha256="",
                file_type=None,
                size_bytes=size,
                storage_path=None,
                parent_file_id=file_id,
            )
        cf_repo.set_status(cid, status="rejected", detail={"reason": reason})
        counts["rejected"] += 1

    for info in infos:
        name = info.filename
        children += 1

        if _is_unsafe(name):
            _add_rejected(name, info.file_size, "unsafe_path")
            continue
        tier = classify(name)
        if tier == "bundle":
            _add_rejected(name, info.file_size, "nested_archive_unsupported")
            continue
        if tier is None:
            _add_rejected(name, info.file_size, "unsupported_type")
            continue
        if info.file_size > MAX_UPLOAD_BYTES:
            _add_rejected(name, info.file_size, "member_too_large")
            continue

        data = zf.read(info)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in ("html", "htm"):
            data = normalize_html(data)
        if not data:
            _add_rejected(name, info.file_size, "empty_member")
            continue

        stored = store_corpus_bytes(corpus_id, name, data)
        existing = _claim_prior(name, stored.sha256)
        if existing:
            child_id = existing["id"]
        else:
            child_id = cf_repo.add(
                corpus_id=corpus_id,
                filename=name,
                sha256=stored.sha256,
                file_type=stored.ext.lstrip(".") or None,
                size_bytes=stored.size_bytes,
                storage_path=stored.storage_path,
                parent_file_id=file_id,
            )
        if sources_repo is not None:
            to_anchor.append((child_id, name, stored.sha256))
        status = ingest_child(child_id)
        counts[status] = counts.get(status, 0) + 1

    # Prune children from a prior run that no longer match (renamed/changed).
    # Whatever is left in `prior_by_key` after every current entry has tried
    # to `_claim_prior` it is, by construction, unmatched — no separate
    # `kept_ids` membership check needed. Each removed row is hard-deleted,
    # so its own `corpus_file_sources` anchor (FK `ondelete="CASCADE"`) and
    # any fact-graph claims (`claims.corpus_file_id -> corpus_files.id ON
    # DELETE CASCADE`) go with it — a subject left with zero claims by that
    # cascade is swept below, same as any other document-deletion path
    # (spec §6). A removed member that was itself a derived table (tabular
    # extension inside the zip) also has its `table_registry` row/parquet
    # purged — before this capability existed, that same reconciliation
    # branch was unreachable in practice: a changed ARCHIVE used to
    # hard-purge every member up front (see
    # `app.api.collections._purge_children_and_content`), so this loop never
    # ran against a real removal; now it does.
    chunks_repo = corpus_chunks_repo()
    removed = [row for bucket in prior_by_key.values() for row in bucket]
    if removed:
        from app.api.collections import _schedule_derived_purge, _sweep_facts_orphans_after_delete

        for row in removed:
            _schedule_derived_purge(corpus_id, row["id"])
            chunks_repo.delete_for_file(row["id"])
            cf_repo.delete(row["id"])
        _sweep_facts_orphans_after_delete(trigger=f"bundle_member_removed:{file_id}")

    # Anchors are written LAST, now that any old row holding the same
    # `_member_stable_id` (a renamed/changed member) is gone — see
    # `to_anchor`'s comment above. Idempotent — refreshes an already-anchored
    # reused row exactly like a no-op, and back-fills the anchor for a member
    # ingested before this capability shipped (no migration needed: the next
    # sync/reingest anchors it). `source_doc_id` is the member's OWN content
    # sha256[:16] — the same citation-key convention the crawler uses for a
    # top-level file (spec §6) — so a producer can cite a member by that id
    # with no `documents[]` entry at all, exactly like an already-resolved
    # top-level doc_id.
    for child_id, name, sha256 in to_anchor:
        try:
            sources_repo.upsert(
                corpus_file_id=child_id,
                corpus_id=corpus_id,
                source_stable_id=_member_stable_id(file_id, name),
                source_doc_id=sha256[:16],
                source_sha256=sha256,
            )
        except Exception as exc:
            if not _is_stable_id_race(exc):
                raise
            # Concurrency guard (review finding 3): two overlapping
            # `ingest_bundle` runs on the SAME archive — e.g. `upload_files`
            # double-fired by a client retry racing the already-scheduled
            # background task — can both mint a FRESH row for the same
            # changed member and then both try to claim the same
            # `_member_stable_id`. `upsert`'s conflict target is
            # `corpus_file_id` (the PK), not the `(corpus_id,
            # source_stable_id)` pair, so the loser of that race hits the
            # unique constraint as what would otherwise be an unhandled
            # `IntegrityError` -> 500. This run's OWN member content is
            # already correctly stored and (re)ingested either way — only
            # the anchor write is skipped — and the very next `ingest_bundle`
            # pass (a routine re-sync) reconciles the loser's now-orphaned
            # duplicate row via the ordinary `(filename, sha256)` prune, so
            # nothing is left permanently inconsistent.
            logger.warning(
                "bundle member anchor race: %s already anchored by a concurrent run file_id=%s member=%r child_id=%s",
                _member_stable_id(file_id, name),
                file_id,
                name,
                child_id,
            )

    detail = {"kind": "bundle", "children": children, **counts}
    if counts["indexed"] > 0:
        cf_repo.set_status(file_id, status="indexed", detail=detail)
        return "indexed"
    cf_repo.set_status(file_id, status="needs_review", detail={**detail, "reason": "no_member_indexed"})
    return "needs_review"
