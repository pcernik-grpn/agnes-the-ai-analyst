"""Shared reservation rule for bundle (zip) member ``source_stable_id``s.

Split out of ``src/ingest/bundle.py`` (the minting side, ``_member_stable_id``)
so the READ/validation side has a home neither the repository layer
(``src/repositories/facts_pg.py``) nor the app layer
(``app/api/collections.py``) needs to reach into the ingestion pipeline
module for — a pure, dependency-free predicate both can import directly.

**Why this exists (security, not tidiness):** ``_member_stable_id`` mints
``"<archive corpus_files.id>!<member path>"`` and ``ingest_bundle`` writes it
into ``corpus_file_sources`` at unpack time. A caller who can only READ a
collection can already see both halves of that string in an ordinary file
listing (``_file_out`` returns ``corpus_files.id`` and ``filename`` for every
row), so the shape is not a secret — the "cannot collide" property the
minting side relies on is a *format convention*, not a barrier against a
caller who deliberately reconstructs it. A caller with collection WRITE
access could otherwise re-upload an unrelated file carrying that exact
``source_stable_id``: ``_upsert_corpus_file`` resolves stable_id first (§6)
and updates the real member row IN PLACE — a silent content substitution
that bypasses ``ingest_bundle`` entirely. Worse, the facts ingest
``documents[]`` path (§7.2) does the identical stable-id-first resolve and
then ``sources_repo.upsert(...)``, whose conflict target is the
``corpus_file_id`` PK — so the same forged stable_id lets a caller overwrite
a real member's ``source_doc_id``, hijacking the citation key so a FUTURE
claim resolves as if grounded in a different, trusted document.

The fix is to reserve the whole shape space at the door: any
caller-supplied ``source_stable_id`` on this shape is refused, at both entry
points, before it is ever resolved or minted. Only ``ingest_bundle`` may
write an anchor on this shape.
"""

from __future__ import annotations


def is_reserved_member_stable_id(source_stable_id: str) -> bool:
    """True when ``source_stable_id`` has the shape only ``ingest_bundle``'s
    ``_member_stable_id`` may mint: ``cf_<hex>!<member path>``.

    Every ``corpus_files.id`` is generated as ``"cf_" + secrets.token_hex(8)``
    (``src/repositories/corpus_files.py::add`` /
    ``corpus_files_pg.py::add``) — a real, producer-supplied top-level
    ``source_stable_id`` follows the crawler's own convention
    (``graph:<driveItem-id>``, ``local:<relpath>``, design spec §6) and never
    starts with ``cf_``. The check deliberately does not pin the trailing hex
    length: a coarse "starts with the reserved prefix AND contains the
    member separator" test reserves the entire shape space regardless of how
    ``corpus_files.id`` generation might change, and a false positive here
    only costs a producer an unusual (and never legitimately needed)
    ``cf_...!...``-shaped stable_id of their own choosing.
    """
    return source_stable_id.startswith("cf_") and "!" in source_stable_id
