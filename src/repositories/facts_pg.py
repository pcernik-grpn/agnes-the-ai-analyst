"""Postgres-only repository for the fact graph over Collections.

facts / fact_aliases / edges / claims / corrections — build order step 2+3
of docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md.

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.facts_repo()``; on a DuckDB-backed instance that factory
call raises ``RequiresPostgresBackend`` (translated to a ``501`` by the
app-wide handler in ``app/main.py``).

**Every read method funnels through the SAME visibility primitives in this
module** (spec §5): a claim counts toward a subject's existence/attrs/quotes
iff its ``corpus_id`` is in the caller's readable-collection set (or the
subject carries an active ``revealed`` correction, which bypasses grants
entirely — spec §4). Never ``can_access_collection`` — see
``app/auth/access.py::accessible_collection_ids`` docstring for why a
restricted ``AgentPrincipal`` would crash or elevate through it.

A nonexistent subject id and a subject with zero readable claims are
**indistinguishable** to every caller (§5 rule 2): both run the exact same
query shape and raise :class:`FactNotFound`, translated to a `404` by the
REST layer — never a `403`.

**Endpoint evidence (spec §4, rev 3.2 — found by the Run P proving run):**
the producer wire contract (§7.0) explicitly permits a node with no evidence
of its own to exist purely to anchor an evidenced edge ("nodes without
evidence are warnings, every edge carries >=1 evidence"). An edge's claim
therefore evidences the relationship AND, implicitly, the existence of its
two endpoints: a FACT subject's existence check runs over its OWN claims
UNION the claims of every edge incident to it that is not itself withheld
(`wrong`/`restricted`) — same corpus-readability predicate, same any/all
`_visibility_mode()` semantics. This applies ONLY to existence — the attrs
projection (`search()`, `collection_facts_summary()`) and the claims listing
(`claims()`) stay OWN-claims-only, so an endpoint-only fact (no own claims
at all) is visible with `attrs: {}` and an empty `claims` list rather than a
`404`. EDGE visibility is unchanged (an edge's own claims only, spec S3);
this is existence propagation for FACTS one hop out over their incident
edges, not the reverse.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import secrets
import threading
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from src.ingest.member_identity import is_reserved_member_stable_id

logger = logging.getLogger(__name__)


# Query-surface caps (spec §12) — the repository enforces these itself
# (defense in depth) even though the REST layer's Pydantic models already
# cap the request shape; a future CLI/MCP caller reaches the same floor.
MAX_SEARCH_LIMIT = 100
MAX_NEIGHBORS_DEPTH = 2
MAX_NEIGHBORS_FANOUT = 100
MAX_NEIGHBORS_RESULT = 500
MAX_SEARCH_FILTERS = 20
# Relationship-shaped read (TCRD-295, spec §12 addendum): `edges()` lists
# every visible edge of ONE type with both endpoints, so a "which X relate to
# which Y" question is one call instead of one `neighbors()` per root. Same
# ceiling as `neighbors()`'s fanout — a page, not a dump.
MAX_EDGES_LIMIT = 100
# `claims()` row cap (TCRD-295). Measured 2026-09-02 on the Cuesta instance:
# an uncapped `fact_claims` on a hub subject returned 18.5k tokens that every
# later step of the turn re-read. Paired with a `limit_applied` signal that
# is computed from the CALLER'S OWN readable set (never from what grants
# hid), exactly as `search()` does, so the cap does not recreate the S6
# shortfall oracle the original no-LIMIT comment guarded against.
MAX_CLAIMS_LIMIT = 200
# Bounded inline evidence (TCRD-295): `include_claims=k` on `edges()`,
# `neighbors()` and `search()` attaches the k NEWEST readable claims per
# subject — computed AFTER audience-variant dedup, inheriting `claims()`'s
# revealed/opaque-document rules — so a relationship answer carries its
# citation without a `fact_claims` round trip per edge. Three ceilings keep
# the payload the thing this exists to shrink: k itself, a total budget per
# response (attached in result order until spent, then `truncated.claims`),
# and a per-quote character cap. `_INLINE_CLAIMS_WINDOW` bounds how many of
# a subject's newest rows are fetched BEFORE dedup: k is exact whenever a
# document carries fewer than window/k audience variants (a handful exist in
# practice), never approximate silently — a subject with more variants than
# that on its newest documents would attach fewer than k, not a wrong one.
MAX_INLINE_CLAIMS_K = 3
MAX_INLINE_CLAIMS_TOTAL = 60
INLINE_QUOTE_MAX_CHARS = 400
_INLINE_CLAIMS_WINDOW = 25
# Hard ceiling on rows `claims()` reads before dedup + cap (bounded DB read
# for a pathological subject; the statement timeout is the other guard).
_CLAIMS_FETCH_CAP = 5_000
# P2 review finding: a 1-char `q` drives a full-scan ILIKE over every
# fact_aliases row with no useful selectivity. Enforced here (repo layer),
# not only in the REST Pydantic model, so an MCP/CLI caller reaching
# `search()` directly cannot bypass it. A blank/whitespace `q` is exempt —
# that already degrades to "no filter" (pre-existing contract).
MIN_SEARCH_Q_LENGTH = 2
# Live finding (2026-09): a 2–3 character `q` is a SUBSTRING of a large share
# of any real alias set (`%ing%` matched 225k of 830k facts on one graph,
# `%ent%` 318k), so as a plain substring it fills `SEARCH_CANDIDATE_CAP` with
# noise on every call. Below this many NORMALIZED characters `q` must instead
# start a name TOKEN of the alias slug — the slug itself or a punctuation-
# delimited part of it (`llr` matches `llr-corp` and `acme-llr`, never
# `fullrange`). Disclosed in the `fact_search` tool docstring, the REST
# docstring and the CLI help; at this length and above the plain substring
# semantics apply unchanged.
SHORT_Q_TOKEN_PREFIX_LENGTH = 4
# Hard ceiling on the candidate set a `q` search ranks and projects (live
# finding, 2026-09): matching via `EXISTS (... fact_aliases ... ILIKE ...)`
# left the planner with no selectivity estimate, it sized the candidate CTE
# at ~416k rows (actual 122) on an ~830k-fact graph and chose full scans of
# `claims` (3.9M rows) and `fact_aliases` (841k) over 122 index probes —
# 5.3 s, i.e. `_STATEMENT_TIMEOUT_MS`, for a three-letter name. Selecting
# candidates as a MATERIALIZED, ranked, LIMIT-ed CTE hands the planner a
# cardinality ceiling it trusts (same query, same instance: 309 ms). The
# effective cap is `max(limit * 4, SEARCH_CANDIDATE_CAP)`; at
# `MAX_SEARCH_LIMIT` the floor dominates, so this constant is the cap.
# Hitting it is disclosed (`candidates_capped: true`) rather than presented
# as a complete ranking, and it is counted over READABLE aliases only, so it
# cannot become an oracle for how many restricted names match (S9).
SEARCH_CANDIDATE_CAP = 500

# Ingest batch caps (spec §7.2): "≤500 documents, ≤5000 claims per request".
# A single document's evidence count over MAX_INGEST_CLAIMS is a protocol
# error (IngestDocumentExceedsClaimCap) rather than a batch-size error — the
# caller cannot fix it by splitting the request, because §7.2 requires a
# `full_documents`-listed document's COMPLETE claim set to arrive together.
MAX_INGEST_DOCUMENTS = 500
MAX_INGEST_CLAIMS = 5000

# Meaningfulness floor for a verbatim quote (spec §8): a plain substring test
# alone accepts ANY fragment that happens to occur literally in the text or
# the document's own identity strings, including one with no evidentiary
# value — a bare file-extension fragment (".pdf") or a lone path separator
# ("/") both pass whenever the document mentions a filename or a date/
# fraction/URL anywhere. A raw length floor alone cannot separate these from
# a legitimate short quote: ".pdf" and "ARR" (a real metric name) are the
# same length once ".pdf"'s leading punctuation is set aside. What
# distinguishes them is not length but SHAPE — ".pdf" is a punctuation-
# fringed fragment, dependent on characters outside the quote, never a
# complete token on its own; "ARR" is not. A constant, not a
# `facts.single_valued_edges`-style `get_value` knob: this guards evidence
# INTEGRITY (can a fabricated/degenerate LLM extraction get past the gate),
# not a per-instance ontology choice, so it is not something an operator
# should be able to loosen. See `_is_meaningful_quote`.
MIN_MEANINGFUL_QUOTE_LENGTH = 2

# The separator `facts_extraction.py::_document_text` joins a document's
# chunks with, to build the ONE string the model actually reads. Named here,
# not re-literal'd, so the two representations can never drift (same
# "imported rather than re-implemented" discipline as `_identity_candidates`/
# `_is_meaningful_quote` below, just in the other direction). Used to widen
# the verbatim gate's substring test to the document's FULL joined text, not
# only one chunk at a time (cost-levers spec 2026-09-02 §2.1(b)/§2.2): a
# quote is still required to be an EXACT substring of real, stored text —
# this only fixes an incomplete definition of "the text the model saw", it
# does not relax exactness. A quote landing entirely inside one chunk is
# unaffected (`any(quote in t for t in texts)` already accepts it); this
# only rescues a quote that genuinely spans what was, to the model, an
# invisible internal split point.
CHUNK_JOIN_SEPARATOR = "\n\n"

# One statement-scoped guard against a runaway traversal on power-law data
# (spec §12 — "the hub-node walk is the query that explodes"). Local to the
# repo, not an operator switch — the caps above are the primary defense.
_STATEMENT_TIMEOUT_MS = 5_000

# `sweep_orphans()`'s grace period (live finding, 2026-09 — see
# migrations/versions/0100_facts_created_at.py for the incident numbers): a
# subject younger than this is never swept, however orphaned it looks right
# now, because it may simply be mid-write in ITS OWN pass. 15 minutes is
# generous relative to a single `ingest_batch` call (seconds, not minutes),
# so it costs nothing to a healthy pass while comfortably outlasting the
# window between one pass's commits. A constant, not a `get_value` operator
# knob, for the same reason `MIN_MEANINGFUL_QUOTE_LENGTH` is one: this
# guards write-path correctness, not an ontology choice — callers that
# genuinely need immediate-delete semantics (a test, or the collections
# file-delete hook, where nothing else could be concurrently orphaning the
# same subject) pass `grace_seconds=0` explicitly.
_ORPHAN_SWEEP_GRACE_S = 15 * 60

# Global (not per-connection) transaction-scoped advisory-lock key
# serializing `sweep_orphans()` itself across every concurrent
# `ingest_batch` pass. Single-bigint `pg_try_advisory_xact_lock` overload —
# distinct from `_SEED_LEASE_ID`/`_REBUILD_LEASE_ID` (src/db_pg.py, the
# session-scoped `pg_advisory_lock` overload) and from
# `_FACTS_LOCK_CLASS_ID` (src/repositories/sharepoint_state_pg.py, a
# two-int `(class_id, hashtext(connection_id))` key scoped to ONE
# connection's pass): the sweep has no per-connection scope of its own — it
# operates over the whole shared fact graph — so there is no natural second
# key to pair it with, and a bare single-bigint key can never collide with
# that two-int form (its packed 64-bit value always has a nonzero high
# 32 bits, this constant's does not).
_SWEEP_LOCK_ID = 0x46414353  # "FACS" packed as an int32

# Transaction-scoped advisory-lock CLASS id serializing
# `_rebuild_one_collection_stats` against a SECOND concurrent rebuild of the
# SAME collection (TCRD-296 gap #73b) — paired two-int form,
# `(_COLLECTION_STATS_LOCK_CLASS_ID, hashtext(corpus_id))`, matching
# `_FACTS_LOCK_CLASS_ID` (src/repositories/sharepoint_state_pg.py)'s own
# reasoning: a bare single-bigint key hashed only from `corpus_id` could in
# principle collide with some OTHER single-bigint advisory lock this
# repository takes (`_SWEEP_LOCK_ID` included); pairing it with a class id
# of its own keeps this lock's namespace disjoint from every other advisory
# lock here, at the cost of nothing. BLOCKING (`pg_advisory_xact_lock`, not
# `pg_try_advisory_xact_lock`) — unlike `sweep_orphans`, where a pass that
# loses the race simply skips its own sweep, two rebuilds of one collection
# must BOTH eventually run (a caller awaiting the result), so this one
# queues rather than no-ops.
_COLLECTION_STATS_LOCK_CLASS_ID = 0x53544154  # "STAT" packed as an int32

# TCRD-296 gap #78 follow-up (Devin Review on #2273): a cheap, in-memory
# "this corpus's fact graph changed" signal for `app/web/router.py`'s
# `collection_facts_summary` TTL cache. Bumped by every write path below
# that can change what that method returns for a corpus — a new/deleted
# claim, a reassigned file, a rebuild, a correction, a merge/split — so a
# write is visible on the very next read regardless of the cache's TTL: the
# TTL only smooths repeated reads of UNCHANGED data (the Files section's
# own pager), never a stale answer after a real write. Process-local, never
# persisted — a restart naturally invalidates everything, which is correct
# (nothing cached to invalidate). NOT bumped by a write that bypasses this
# repository entirely (raw SQL against `claims`/`edges`/`corrections`, e.g.
# a test fixture or an out-of-band data fix) — there is no application-level
# write path to hook in that case, the same limitation `fact_collection_
# stats` itself already has; such a caller must invalidate explicitly via
# `FactsPgRepository.invalidate_corpus_facts_cache`.
_corpus_facts_version_lock = threading.Lock()
_corpus_facts_version: Dict[str, int] = {}


def _bump_corpus_facts_version(*corpus_ids: Optional[str]) -> None:
    with _corpus_facts_version_lock:
        for cid in corpus_ids:
            if cid:
                _corpus_facts_version[cid] = _corpus_facts_version.get(cid, 0) + 1


def _clear_all_corpus_facts_versions() -> None:
    """Coarse invalidation for a write whose affected corpora are not
    cheaply known here — a correction is scoped by subject id, not corpus,
    and a subject's claims can span more than one. Corrections are a rare,
    admin-triggered path, so invalidating every corpus's cache entry rather
    than tracing the exact affected set is the correct, simple trade-off."""
    with _corpus_facts_version_lock:
        _corpus_facts_version.clear()


class FactNotFound(RuntimeError):
    """A subject that does not exist OR has no readable claim (spec §5 rule
    2) — the caller of this repository must translate this to a clean `404`
    and never leak which of the two applied."""

    def __init__(self, subject_id: str) -> None:
        self.subject_id = subject_id
        super().__init__(f"fact subject {subject_id!r} not found")


class FactsQueryTimeout(RuntimeError):
    """A read statement outlived ``_STATEMENT_TIMEOUT_MS`` (Postgres
    ``57014 query_canceled``, lock waits included). The message IS the
    caller-facing hint (command-ux.md: an error names the next step): the
    REST layer answers ``504 {"reason": "facts_search_timeout", "hint":
    <message>}`` and the MCP tool re-raises it as a ``ValueError``, so the
    model reads what to do instead of the raw driver text — the live
    failure this closes surfaced ``(psycopg.errors.QueryCanceled) canceling
    statement due to statement timeout`` in the tool result, which told it
    nothing it could act on (2026-09)."""

    reason = "facts_search_timeout"


_PG_QUERY_CANCELED_SQLSTATE = "57014"


def _is_statement_timeout(exc: sa.exc.DBAPIError) -> bool:
    """True when a SQLAlchemy-wrapped driver error is Postgres cancelling the
    statement because ``statement_timeout`` fired."""
    orig = exc.orig
    return getattr(orig, "sqlstate", None) == _PG_QUERY_CANCELED_SQLSTATE or type(orig).__name__ == "QueryCanceled"


def _search_timeout_message(timeout_ms: int) -> str:
    return (
        f"The fact search did not finish within {timeout_ms / 1000:g} s and was cancelled. Narrow `q` to a "
        "longer, more specific name (a short or common fragment matches too many names), add `type`, "
        "or lower `limit`, then retry."
    )


def _regex_literal(text: str) -> str:
    """Escape ``text`` for use as a LITERAL inside a Postgres ARE pattern:
    every ASCII non-alphanumeric character gets a backslash (in an ARE a
    backslash before a non-alphanumeric character is that character,
    verbatim), so a caller-supplied ``q`` can never contribute regex syntax.
    Non-ASCII characters are left alone — none is regex syntax, and a
    backslash before a locale-alphanumeric one would be an invalid escape.
    The pattern this feeds is a fixed boundary class plus this literal, so
    it is linear-time by construction (security playbook: no ReDoS surface
    over untrusted text)."""
    return "".join(("\\" + ch) if (ch.isascii() and not ch.isalnum()) else ch for ch in text)


class IngestBatchTooLarge(RuntimeError):
    """A whole-batch size cap (§7.2: ≤500 documents / ≤5000 claims) was
    exceeded. ``detail`` is the itemized reason the REST layer serializes
    on a `413`."""

    def __init__(self, detail: Dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(str(detail))


class IngestDocumentExceedsClaimCap(RuntimeError):
    """A SINGLE document's evidence count exceeds ``MAX_INGEST_CLAIMS`` —
    never split (§7.2): the caller must shrink that document's own claim
    set, not merely paginate the batch. Translated to a `422`."""

    def __init__(self, doc_id: str, count: int) -> None:
        self.doc_id = doc_id
        self.count = count
        super().__init__(f"document {doc_id!r} carries {count} claims (cap {MAX_INGEST_CLAIMS})")


class IngestUnresolvedDocIds(RuntimeError):
    """``documents`` was omitted but not every referenced ``doc_id``
    already resolves (§7.2) — the whole batch is rejected, itemized.
    Translated to a `400`."""

    def __init__(self, unresolved: List[str]) -> None:
        self.unresolved = unresolved
        super().__init__(f"unresolved doc_ids: {unresolved}")


class IngestReservedStableId(RuntimeError):
    """A ``documents[].stable_id`` matched the reserved bundle-member anchor
    shape (``src.ingest.member_identity.is_reserved_member_stable_id`` —
    ``cf_<hex>!<member path>``, minted only by ``ingest_bundle``). Refused
    up front, before ANY document in this batch is resolved or upserted: the
    identical stable-id-first resolve this method's own ``documents[]`` loop
    performs would otherwise let a caller overwrite a real member's
    ``corpus_file_sources`` row via ``sources_repo.upsert``'s
    ``corpus_file_id``-keyed conflict target — hijacking the member's
    ``source_doc_id`` citation key so a FUTURE claim resolves as if grounded
    in a different, trusted document. Translated to a `400`, whole batch
    rejected (no partial write), the offending stable_ids itemized."""

    def __init__(self, stable_ids: List[str]) -> None:
        self.stable_ids = stable_ids
        super().__init__(f"reserved stable_ids in documents[]: {stable_ids}")


class EdgeEndpointMissing(RuntimeError):
    """:meth:`FactsPgRepository.create_edge` was rejected by Postgres's
    foreign-key constraint on ``edges.src``/``edges.dst`` (SQLSTATE
    ``23503``, foreign_key_violation) — the endpoint fact resolved fine
    moments earlier but no longer exists in ``facts`` by the time this
    INSERT ran. Two live triggers observed in production (2026-09): a
    concurrent ``ingest_batch`` call's own end-of-call ``sweep_orphans()``
    garbage-collecting a just-created, not-yet-evidenced fact before this
    call's edge could anchor it, and two facts-extraction passes racing to
    merge/deduplicate the same entity. Neither is a caller bug — it is
    inherent to several passes writing into one shared fact graph
    concurrently — so :meth:`FactsPgRepository.ingest_batch`'s edge loop
    catches this, counts it (``edges_skipped_missing_endpoint`` in the
    ingest report) and skips only the one affected edge rather than
    failing the whole batch."""

    def __init__(self, *, src: str, type: str, dst: str) -> None:
        self.src = src
        self.type = type
        self.dst = dst
        super().__init__(f"edge endpoint missing: {src} -[{type}]-> {dst}")


def _decode_jsonb(value: Any) -> Any:
    """PG JSONB columns come back as ``str`` through a raw ``sa.text()``
    query on this driver stack (see ``src/repositories/knowledge_pg.py``);
    decode defensively so an already-adapted value (dict/list) passes
    through unchanged."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _visibility_mode() -> str:
    from app.switches import switch_value

    mode = switch_value("facts_visibility_mode")
    return mode if mode == "all_evidence" else "any_evidence"


_COLLECTION_STATS_WARNED = False


def _warn_collection_stats_unavailable_once() -> None:
    """TCRD-296 E.21: logged once per process — `_bump_collection_stats_
    on_new_claim` degraded to a no-op because `fact_collection_stats`/
    `*_collection_membership` aren't reachable (an instance mid-migration,
    or the deliberate pre-0104 schema pin in `tests/db_pg/
    test_facts_read_pg.py`'s S9 backfill tests). Every reader of these
    tables already falls back to the original `claims` scan on its own —
    this line exists purely so an operator notices "the summary isn't
    being maintained" rather than silently getting the slow path forever."""
    global _COLLECTION_STATS_WARNED
    if not _COLLECTION_STATS_WARNED:
        _COLLECTION_STATS_WARNED = True
        logger.warning(
            "fact_collection_stats/*_collection_membership unavailable — "
            "collection-stats summary not maintained for this write; readers "
            "fall back to the original claims scan until "
            "`agnes admin facts stats rebuild` runs on a fully-migrated database"
        )


def _readable_ids(caller) -> Optional[frozenset]:
    """The caller's readable-collection set. ``None`` = admin (no filter).

    Delegates entirely to :func:`app.auth.access.accessible_collection_ids`
    — the ONE sanctioned primitive (spec §5); never
    ``can_access_collection``.
    """
    from app.auth.access import accessible_collection_ids

    ids = accessible_collection_ids(caller)
    if ids is None:
        return None
    return frozenset(ids)


def _audience_context(caller, readable: Optional[frozenset]) -> Tuple[List[str], List[str]]:
    """The two audience-selector bind lists every non-admin read threads
    alongside ``:readable`` into :meth:`FactsPgRepository._visibility_predicate`
    (2026-08-30 sharepoint-acl-mirroring plan, Task 10; spec §4.2-§4.4):

    - ``tiered_hidden`` — collection ids to hide an UNTAGGED claim from
      entirely, i.e. every audience-tiered collection (
      :func:`src.audience_classes.tiered_collection_ids`), but ONLY under
      the ``must_not`` guarantee mode (design Q7 fork, the SAME
      ``acl_guarantee_mode`` switch ``connectors/sharepoint/acl_sync.py``
      reads) — enabling tiers on a scope is a gated re-index precisely so no
      untagged full-detail claim survives it (spec §4.4); ``should_not``
      leaves untagged claims unrestricted within their collection, matching
      today's pre-audience behavior. Empty under ``should_not`` or for an
      admin (the caller of this function skips it entirely then).
    - ``audience_pairs`` — ``"{corpus_id}:{class}"`` strings for every
      audience class ``caller`` holds on every TIERED collection in
      ``readable`` (:func:`src.audience_classes.audience_classes_for_caller`)
      — the tagged-claim half of the selector.

    ``readable=None`` (admin, per :func:`_readable_ids`) short-circuits to
    ``([], [])`` — the caller of this function must never call it for an
    admin in the first place (:meth:`_visibility_predicate` returns ``TRUE``
    unconditionally then, referencing neither bind), but returning empty
    lists rather than raising keeps this a plain, total function."""
    if readable is None:
        return [], []

    from app.switches import switch_value
    from src.audience_classes import audience_classes_for_caller, tiered_collection_ids

    tiered_hidden = list(tiered_collection_ids()) if switch_value("acl_guarantee_mode") == "must_not" else []
    per_collection = audience_classes_for_caller(caller, readable)
    audience_pairs = [f"{corpus_id}:{cls}" for corpus_id, classes in per_collection.items() for cls in classes]
    return tiered_hidden, audience_pairs


def _class_rank_map() -> Dict[str, Dict[str, int]]:
    """``{corpus_id: {class_name: rank}}``, rank 0 = most privileged — the
    per-corpus privilege order :func:`src.audience_classes.audience_class_map`
    already carries (wizard-persisted, most-privileged first), reshaped for
    :func:`_pick_most_privileged`'s O(1) rank lookup. A collection absent
    from the result is non-tiered (or unknown); :func:`_pick_most_privileged`
    treats a class name it can't find here the same as untagged — the
    least-privileged floor, never a crash."""
    from src.audience_classes import audience_class_map

    return {
        corpus_id: {name: idx for idx, (name, _group_ids) in enumerate(classes)}
        for corpus_id, classes in audience_class_map().items()
    }


# Sentinel rank for "no better-known privilege" — anything untagged, or
# tagged with a class this instance's audience_class_map no longer
# recognizes (renamed/removed since the claim was written), floors here
# rather than raising. One less than the true floor is reserved so a KNOWN
# tagged class always outranks an unrecognized one, which in turn always
# outranks untagged (spec §4.2: untagged is the least-privileged variant).
_UNTAGGED_RANK = 10**9
_UNKNOWN_CLASS_RANK = _UNTAGGED_RANK - 1


def _pick_most_privileged(rows: List[Any], class_rank: Dict[str, Dict[str, int]]) -> List[Any]:
    """Among CLAIM rows (each a mapping carrying at least ``fact_id``,
    ``edge_id``, ``corpus_file_id``, ``corpus_id`` and ``audience`` — a
    fixed field absent from the row's own subject reads as ``None``, so a
    ``claims()`` result where ``fact_id``/``edge_id`` is constant for the
    whole list still groups correctly by ``corpus_file_id`` alone), keep
    only the rows at the BEST (lowest) privilege rank within each
    ``(fact_id, edge_id, corpus_file_id)`` group (spec §4.2's "most
    privileged variant" rule, 2026-08-30 sharepoint-acl-mirroring plan, Task
    10).

    Deliberately rank-floor-based, not "keep exactly one row per group": a
    group where EVERY row is untagged (all rank at the shared
    ``_UNTAGGED_RANK`` floor) keeps ALL of them — two genuinely distinct
    untagged quotes about the same file are independent evidence, never
    collapsed into one. A group MIXING an untagged row with a tagged one
    (or several tagged tiers) keeps only the row(s) at the single best rank
    present, dropping the rest — the "$20k vs <redacted>" shape. Order-
    preserving over the input."""

    def _rank(row: Any) -> int:
        audience = row.get("audience")
        if audience is None:
            return _UNTAGGED_RANK
        return class_rank.get(row.get("corpus_id"), {}).get(audience, _UNKNOWN_CLASS_RANK)

    def _key(row: Any) -> Tuple[Any, Any, Any]:
        return (row.get("fact_id"), row.get("edge_id"), row.get("corpus_file_id"))

    best_rank: Dict[Tuple[Any, Any, Any], int] = {}
    for row in rows:
        key = _key(row)
        rank = _rank(row)
        if key not in best_rank or rank < best_rank[key]:
            best_rank[key] = rank
    return [row for row in rows if _rank(row) == best_rank[_key(row)]]


def _add_path_component_candidates(candidates: Set[str], value: str) -> None:
    """Decompose ``value`` (a stored ``path`` OR a stored ``filename`` — see
    ``_identity_candidates``) into whole-unit candidates and add them to
    ``candidates`` in place: the string itself; every single whole
    ``/``-separated component (a folder name, or the final segment); and
    every CONTIGUOUS run of whole components (e.g. ``"folder/name.ext"``) —
    the run ending at the final segment also gets an extension-stripped
    variant. A separator-free ``value`` (the ordinary case) decomposes to
    exactly ``{value, stem}``, identical to the pre-bundle-support shape."""
    candidates.add(value)
    parts = [p for p in value.split("/") if p]
    n = len(parts)
    for i in range(n):
        for j in range(i + 1, n + 1):
            candidates.add("/".join(parts[i:j]))
            if j == n:  # this run ends at the final segment
                last = parts[j - 1]
                dot = last.rfind(".")
                if dot > 0:
                    candidates.add("/".join(parts[i : j - 1] + [last[:dot]]))


def _identity_candidates(filename: Optional[str], path: Optional[str]) -> Set[str]:
    """Whole-unit identity-evidence candidates for the verbatim gate (spec
    §8, P0 review finding). A quote counts as identity-grounded evidence
    only when it EQUALS one of these — never merely CONTAINS one as a
    substring, which let a fabricated quote self-certify on any fragment of
    the document's own name (a bare ``".pptx"``, a stray ``"/"``, a 2-char
    slice). Both ``filename`` and ``path`` are decomposed the SAME way
    (``_add_path_component_candidates``) — an ordinary ``filename`` has no
    ``/`` so this changes nothing for it, but a bundle member's stored
    ``filename`` IS itself a path (``src/ingest/bundle.py`` stores the
    archive-relative member path there, with no ``path`` at all), so
    decomposing only ``path`` silently starved that case of any component
    candidates (live cross-PR regression: a zip member's own folder name
    was rejected). Candidates: each of ``filename``/``path`` in full; each
    with its extension stripped; every single whole ``/``-separated
    component of each (a folder name, or the final segment); and every
    CONTIGUOUS run of whole components (e.g. ``"folder/filename.ext"``, the
    folder+filename shape a `part_of` edge legitimately cites) — the run
    ending at the final segment also gets an extension-stripped variant. No
    normalization anywhere: matches the chunk-text comparison exactly (an
    NFC/NFD quote fails identically on both sides)."""
    candidates: Set[str] = set()
    if filename:
        _add_path_component_candidates(candidates, filename)
    if path:
        _add_path_component_candidates(candidates, path)
    candidates.discard("")
    return candidates


_WORD_CHAR_RE = re.compile(r"\w", re.UNICODE)


def _is_meaningful_quote(quote: str) -> bool:
    """Verbatim-gate meaningfulness floor (spec §8; see
    ``MIN_MEANINGFUL_QUOTE_LENGTH`` for the design rationale). Two
    independent conditions, each closing a different degenerate shape:

    1. **Length** — the trimmed quote must be at least
       ``MIN_MEANINGFUL_QUOTE_LENGTH`` characters. Closes a bare single
       character — trivially "starts with a word character" per condition 2
       below, but still not specific enough to be evidence of anything — and
       a run of whitespace, which strips to zero.
    2. **Shape** — the trimmed quote must START with a word character
       (``\\w``: letter, digit, or underscore, Unicode-aware). Closes a
       quote that is entirely punctuation (``/``) AND one that is a
       punctuation-fringed fragment of a longer token (``.pdf`` — the
       leading "." can never be a complete word's own left edge; a real
       sentence never begins mid-token) — both shapes a raw length or
       word-character-count floor cannot tell apart from a short legitimate
       quote of the same length (an acronym, a ticker, a year, a product
       name: ".pdf" and "ARR" are the same length once ".pdf"'s leading
       punctuation is set aside).

       Deliberately checks only the START, not the end: a quote's trailing
       character is routinely punctuation for an entirely ordinary reason —
       it is citing a whole sentence or clause ("Acme Corp is the client.",
       "the engagement is on schedule;") — and requiring a clean end as well
       rejected that common, legitimate shape outright. A quote beginning
       mid-token has no such innocent reading; sentence-final punctuation is
       a closing delimiter of the unit actually quoted, leading punctuation
       with nothing before it in the quote is not.

    Deliberately does NOT require a minimum number of word characters, or
    forbid internal/trailing punctuation — "N/A", "3.14", and "Acme Corp is
    the client." all read fine as evidence.
    """
    stripped = quote.strip()
    if len(stripped) < MIN_MEANINGFUL_QUOTE_LENGTH:
        return False
    return bool(_WORD_CHAR_RE.match(stripped[0]))


def _single_valued_edge_types() -> frozenset:
    """Edge types a src fact should carry exactly one LIVE dst for (spec
    §7.3) — a second distinct dst is a reconciliation problem, not a fact.
    Configurable via ``facts.single_valued_edges`` in instance.yaml (plain
    data, not a bool/select toggle, so this is an ordinary ``get_value``
    read rather than a switch registry entry); default ``["owned_by",
    "for_client"]``. A malformed (non-list) override falls back to the
    default rather than silently disabling detection."""
    from app.instance_config import get_value

    default = ["owned_by", "for_client"]
    configured = get_value("facts", "single_valued_edges", default=default)
    if not isinstance(configured, (list, tuple)):
        configured = default
    return frozenset(str(t) for t in configured)


# ---------------------------------------------------------------------------
# possible_duplicate_of — SYSTEM-proposed candidates (entity-resolution
# review, spec's own two-pass split: extraction is per-document and
# stateless, so a cross-document duplicate can only be caught HERE, at
# ingest, never by the extraction pass itself — see the ontology's own
# `entity_resolution` convention, "merge on exact normalized slug only... a
# probable-but-unsure match is never auto-merged, emit a
# `possible_duplicate_of` edge for human review"). `ingest_batch` calls
# `_duplicate_candidate_reason` for every NEWLY minted fact against every
# OTHER alias of the same type already on file; a hit becomes a
# `possible_duplicate_of` edge through the exact same review-item mechanism
# a producer's own proposal uses (`_review_items_for_corpus`,
# `collection_facts_summary`) — a second SOURCE for that edge type, not a
# new review surface. This NEVER merges anything on its own initiative —
# `merge_facts` stays the only path that folds two facts into one, and
# that remains an explicit, separate, human-triggered call.
# ---------------------------------------------------------------------------

#: Minimum combined character length of the SHORTER slug's tokens for the
#: name-token-prefix rule (below) to fire — guards against a bare 1-3 char
#: stub ("co", "hq") trivially prefix-matching almost anything.
_MIN_PREFIX_TOKEN_CHARS = 4
#: Same guard for the near-identical-spelling rule below — shorter strings
#: make an edit-distance-<=1 match far more likely to be coincidence than
#: a typo in one of the two extractions.
_MIN_TYPO_CHARS = 5


def _levenshtein_distance(a: str, b: str) -> int:
    """Standard edit distance (insert/delete/substitute), iterative DP.

    Both inputs here are single ontology-slug tokens — a handful of
    characters, never document text — so O(len(a)*len(b)) is trivial and
    no third-party dependency is worth adding for it.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


def _duplicate_candidate_reason(slug_a: str, slug_b: str) -> Optional[str]:
    """A short, human-readable reason two SAME-TYPE natural-key slugs
    (the part after ``type:``) might denote one real-world entity under
    different names — ``None`` when neither narrow, deterministic check
    below fires.

    This never decides identity. A hit here only ever earns a
    `possible_duplicate_of` edge — a PROPOSAL for a human (spec §7.2's
    review-item mechanism), the same as when the extraction model
    volunteers one inline. The caller must not, and does not, use this to
    skip alias creation or fold two facts into one.

    Two checks, both pattern-only — no wordlist, no ontology-specific
    vocabulary, so this module stays agnostic to what an instance's node
    types are actually called:

    - **name-token prefix**: one slug's ``-``-separated tokens are an
      exact, in-order, STRICT prefix of the other's (``acme`` /
      ``acme-group``) — the shape of "a short form vs. a longer, more
      qualified name for the same thing", which is exactly what varies
      across documents written by different people, departments, or
      times. Two names that are merely the SAME length and share a
      leading word (``acme-east`` / ``acme-west``) are not a prefix
      relationship and do not match.
    - **near-identical single-token spelling**: both slugs are ONE token
      and differ by an edit distance of at most 1 (``acmee`` / ``acme``)
      — a probable typo in one of the two extractions.

    Both are cheap and structural, and neither is risk-free on its own (a
    common short word, or two genuinely different short names one edit
    apart, can still match) — see the caller's per-fact match cap
    (`FactsPgRepository._MAX_DUPLICATE_CANDIDATES_PER_FACT`) for the
    mitigation this function does not attempt itself: a token shared by
    an unusually large number of existing aliases is far more likely a
    common word than N variants of one entity, so the caller skips
    proposing anything for it rather than guessing which pair(s) matter.
    """
    if slug_a == slug_b:
        return None
    tokens_a = slug_a.split("-")
    tokens_b = slug_b.split("-")
    shorter, longer = (tokens_a, tokens_b) if len(tokens_a) <= len(tokens_b) else (tokens_b, tokens_a)
    if len(shorter) < len(longer) and longer[: len(shorter)] == shorter:
        if len("".join(shorter)) >= _MIN_PREFIX_TOKEN_CHARS:
            return "name-token prefix of a longer/more-qualified name"
    if len(tokens_a) == 1 and len(tokens_b) == 1:
        shortest_len = min(len(slug_a), len(slug_b))
        if shortest_len >= _MIN_TYPO_CHARS and abs(len(slug_a) - len(slug_b)) <= 1:
            if _levenshtein_distance(slug_a, slug_b) <= 1:
                return "near-identical spelling (possible typo)"
    return None


class FactsPgRepository:
    #: Cap on how many existing aliases may match ONE newly created fact
    #: via `_duplicate_candidate_reason` before `_propose_duplicate_
    #: candidates` proposes anything for it at all. A token shared by more
    #: matches than this is far more likely a common short word (a first
    #: name, a generic business word) than N variants of the SAME entity —
    #: the two rules above are meant to produce a narrow candidate set a
    #: human can actually work through, not a similarity search, so this
    #: instance skips the whole group rather than guessing which pair(s)
    #: to keep.
    _MAX_DUPLICATE_CANDIDATES_PER_FACT = 4

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @contextlib.contextmanager
    def _write_conn(self, conn: Optional[Connection] = None) -> Iterator[Connection]:
        """Yield a connection for one or more write statements.

        When ``conn`` is given, the caller already owns an open transaction
        (typically ``self._engine.begin()`` in ``ingest_batch``) — yield it
        as-is and leave commit/rollback entirely to the caller, so several
        of the write helpers below (``create_fact``, ``add_alias_source``,
        ``create_edge``, ``add_claim``) can be composed into ONE atomic
        transaction instead of each opening its own. When omitted (every
        caller outside ``ingest_batch``, and ``ingest_batch`` itself before
        this atomicity fix), open-and-commit a fresh single-statement
        transaction exactly as each of these methods always did — an
        unspecified ``conn`` leaves this method's behavior byte-for-byte
        unchanged."""
        if conn is not None:
            yield conn
            return
        with self._engine.begin() as new_conn:
            yield new_conn

    # ------------------------------------------------------------------
    # internal write/seed methods — minimal and obviously correct; the
    # ingest task (build order step 4) builds the real write-path protocol
    # (verbatim gate, union/replace modes, run report) on top of these.
    # ------------------------------------------------------------------

    def create_fact(
        self,
        *,
        type: str,
        natural_key: Optional[str] = None,
        corpus_id: Optional[str] = None,
        conn: Optional[Connection] = None,
    ) -> str:
        """``corpus_id`` is OPTIONAL provenance for the inline alias
        (security hardening, see :meth:`add_alias_source`): the corpus whose
        evidence justifies showing ``natural_key`` to a caller who cannot
        read every corpus this fact ends up carrying claims from. Ignored
        when ``natural_key`` is absent.

        ``conn`` — see :meth:`_write_conn`: pass an already-open transaction
        to fold this fact's creation into a larger atomic write (e.g.
        ``ingest_batch``'s per-node transaction, which also writes that
        node's own evidence); omit it for the pre-existing
        one-fact-one-transaction behavior."""
        fact_id = "f_" + secrets.token_hex(8)
        with self._write_conn(conn) as c:
            c.execute(sa.text("INSERT INTO facts (id, type) VALUES (:id, :type)"), {"id": fact_id, "type": type})
            if natural_key:
                c.execute(
                    sa.text(
                        "INSERT INTO fact_aliases (fact_id, type, natural_key) VALUES (:fid, :type, :nk) "
                        "ON CONFLICT (type, natural_key) DO UPDATE SET fact_id = EXCLUDED.fact_id"
                    ),
                    {"fid": fact_id, "type": type, "nk": natural_key},
                )
                if corpus_id:
                    c.execute(
                        sa.text(
                            "INSERT INTO fact_alias_sources (type, natural_key, corpus_id) "
                            "VALUES (:type, :nk, :corpus_id) ON CONFLICT DO NOTHING"
                        ),
                        {"type": type, "nk": natural_key, "corpus_id": corpus_id},
                    )
        return fact_id

    def add_alias(self, *, fact_id: str, type: str, natural_key: str, corpus_id: Optional[str] = None) -> None:
        """``corpus_id`` is OPTIONAL provenance (security hardening, see
        :meth:`add_alias_source`) — pass it when the caller knows which
        corpus's evidence backs this exact alias string. Omitting it is
        legal (mirrors the pre-hardening signature) but means this alias
        stays admin-only-visible until some corpus is recorded for it."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO fact_aliases (fact_id, type, natural_key) VALUES (:fid, :type, :nk) "
                    "ON CONFLICT (type, natural_key) DO UPDATE SET fact_id = EXCLUDED.fact_id"
                ),
                {"fid": fact_id, "type": type, "nk": natural_key},
            )
            if corpus_id:
                conn.execute(
                    sa.text(
                        "INSERT INTO fact_alias_sources (type, natural_key, corpus_id) "
                        "VALUES (:type, :nk, :corpus_id) ON CONFLICT DO NOTHING"
                    ),
                    {"type": type, "nk": natural_key, "corpus_id": corpus_id},
                )

    def add_alias_source(
        self, *, type: str, natural_key: str, corpus_id: str, conn: Optional[Connection] = None
    ) -> None:
        """Record that ``corpus_id``'s evidence contributed to minting/
        reinforcing the alias ``(type, natural_key)`` (security hardening —
        module docstring's alias-visibility rule, spec §5 extended to
        names). Insert-only, ``ON CONFLICT DO NOTHING``: the provenance set
        for one alias only ever GROWS, as more corpora independently
        re-derive the same literal string (spec §7.2's deterministic
        node-id contract) — it never shrinks except via cascade when the
        alias itself is deleted (fact orphaned, see ``sweep_orphans``).
        A no-op if the alias row doesn't exist yet — callers that mint the
        alias mid-ingest (:meth:`_resolve_alias`) always create it first.

        ``conn`` — see :meth:`_write_conn`: pass an already-open transaction
        to fold this write into a larger atomic unit (``ingest_batch``'s
        per-node/per-edge transaction); omit it for the pre-existing
        own-transaction behavior."""
        with self._write_conn(conn) as c:
            c.execute(
                sa.text(
                    "INSERT INTO fact_alias_sources (type, natural_key, corpus_id) "
                    "SELECT CAST(:type AS TEXT), CAST(:nk AS TEXT), CAST(:corpus_id AS TEXT) WHERE EXISTS ("
                    "  SELECT 1 FROM fact_aliases WHERE type = :type AND natural_key = :nk"
                    ") ON CONFLICT DO NOTHING"
                ),
                {"type": type, "nk": natural_key, "corpus_id": corpus_id},
            )

    def create_edge(self, *, src: str, type: str, dst: str, conn: Optional[Connection] = None) -> str:
        """``conn`` — see :meth:`_write_conn`: pass an already-open
        transaction to fold this edge's creation into a larger atomic
        write (``ingest_batch``'s per-edge transaction, which also writes
        the edge's own evidence); omit it for the pre-existing
        one-edge-one-transaction behavior.

        Raises :class:`EdgeEndpointMissing` when Postgres rejects the
        INSERT on the ``src``/``dst`` foreign key (SQLSTATE ``23503``,
        foreign_key_violation) — see that exception's docstring for why
        this happens and how ``ingest_batch`` handles it. Any OTHER
        integrity error re-raises unchanged; this only narrows the one
        known, expected race."""
        edge_id = "e_" + secrets.token_hex(8)
        with self._write_conn(conn) as c:
            try:
                c.execute(
                    sa.text(
                        "INSERT INTO edges (id, src, type, dst) VALUES (:id, :src, :type, :dst) "
                        "ON CONFLICT (src, type, dst) DO NOTHING"
                    ),
                    {"id": edge_id, "src": src, "type": type, "dst": dst},
                )
            except sa.exc.IntegrityError as exc:
                if getattr(exc.orig, "sqlstate", None) == "23503":
                    raise EdgeEndpointMissing(src=src, type=type, dst=dst) from exc
                raise
            row = (
                c.execute(
                    sa.text("SELECT id FROM edges WHERE src = :src AND type = :type AND dst = :dst"),
                    {"src": src, "type": type, "dst": dst},
                )
                .mappings()
                .first()
            )
        assert row is not None
        return row["id"]

    def add_claim(
        self,
        *,
        fact_id: Optional[str] = None,
        edge_id: Optional[str] = None,
        corpus_file_id: str,
        corpus_id: str,
        file_sha256: str,
        quote: str,
        attrs: Optional[dict] = None,
        document_date: Optional[date] = None,
        audience: Optional[str] = None,
        conn: Optional[Connection] = None,
    ) -> Optional[str]:
        """Insert a claim; ``ON CONFLICT ... DO NOTHING`` on the (subject,
        corpus_file_id, quote_hash) functional unique index makes a replay
        (spec §7.2's union-mode idempotency) a true no-op. Returns the new
        claim id, or ``None`` when this exact triple already existed — the
        ingest write path (build order step 4) uses that to count
        ``claims_written`` accurately across a replayed batch; no other
        caller (read-path fixtures) inspects the return value.

        ``conn`` — see :meth:`_write_conn`: pass an already-open
        transaction to fold this claim into a larger atomic write
        (``ingest_batch``'s per-node/per-edge transaction, alongside the
        fact/edge it evidences); omit it for the pre-existing
        one-claim-one-transaction behavior.

        ``audience`` (Task 10, spec §4.2) is an optional index-time variant
        tag — ``None`` (the default) stays unrestricted-within-collection,
        today's pre-audience behavior. Format validation (the
        ``^[a-z0-9_-]{1,64}$`` shape) is the CALLER's job (``app/api/facts.py``
        rejects a malformed wire value with a 422 before this ever runs) —
        this method stores whatever it is given verbatim, matching every
        other claim field here.

        The ``audience`` column is referenced in the INSERT only when a
        caller actually passes one — ``tests/db_pg/test_facts_read_pg.py``'s
        S9 backfill tests deliberately pin the Alembic chain to
        ``0083_ingest_runs_source_urls`` (before this column existed) and
        seed legacy-shaped data through this exact method; a hardcoded
        ``audience`` column reference would break that schema-version
        pinning for a value that's ``NULL`` either way. Storing ``None``
        by omitting the column has the identical on-disk result as storing
        it explicitly (the column's own default is ``NULL``), so this is
        never a behavior change against a fully-migrated schema — only
        against one older than 0086."""
        if (fact_id is None) == (edge_id is None):
            raise ValueError("add_claim requires exactly one of fact_id/edge_id")
        claim_id = "c_" + secrets.token_hex(8)
        quote_hash = hashlib.sha256(quote.encode("utf-8")).hexdigest()[:16]
        columns = (
            "id, fact_id, edge_id, corpus_file_id, corpus_id, file_sha256, attrs, quote, quote_hash, document_date"
        )
        placeholders = (
            ":id, :fact_id, :edge_id, :corpus_file_id, :corpus_id, :file_sha256, "
            "CAST(:attrs AS JSONB), :quote, :quote_hash, :document_date"
        )
        params: Dict[str, Any] = {
            "id": claim_id,
            "fact_id": fact_id,
            "edge_id": edge_id,
            "corpus_file_id": corpus_file_id,
            "corpus_id": corpus_id,
            "file_sha256": file_sha256,
            "attrs": json.dumps(attrs or {}),
            "quote": quote,
            "quote_hash": quote_hash,
            "document_date": document_date,
        }
        if audience is not None:
            columns += ", audience"
            placeholders += ", :audience"
            params["audience"] = audience
        with self._write_conn(conn) as c:
            result = c.execute(
                sa.text(
                    f"INSERT INTO claims ({columns}) VALUES ({placeholders}) "
                    "ON CONFLICT (COALESCE(fact_id, edge_id), corpus_file_id, quote_hash) DO NOTHING"
                ),
                params,
            )
            if result.rowcount:
                # TCRD-296 E.21: maintain the collection-stats summary
                # incrementally on this hot path — see "Collection stats
                # summary" below. A replayed claim (ON CONFLICT DO NOTHING,
                # rowcount 0) changes nothing, so nothing to bump either.
                self._bump_collection_stats_on_new_claim(
                    c,
                    fact_id=fact_id,
                    edge_id=edge_id,
                    corpus_id=corpus_id,
                    corpus_file_id=corpus_file_id,
                    new_claim_id=claim_id,
                )
        return claim_id if result.rowcount else None

    # ------------------------------------------------------------------
    # Collection stats summary (TCRD-296 synthesis E.21) — a maintained
    # index over "which fact/edge has a claim in which collection" and
    # "how big is this collection", so the read surface (facets/type-map,
    # the Library index, admin graph counts) never re-derives candidacy by
    # scanning `claims` per request. Three legs:
    #
    # * `_bump_collection_stats_on_new_claim` — the HOT INSERT path, called
    #   from `add_claim` for every genuinely new claim. Deliberately
    #   incremental (a handful of tiny, indexed point lookups scoped to ONE
    #   subject or ONE document, never a corpus-wide scan) — ingest
    #   throughput must not regress just because this summary now exists.
    # * `_decrement_collection_stats_for_deleted_claims` — the per-file
    #   DELETE path, called from `delete_claims_for_file` for every claim a
    #   re-extraction pass just removed. The exact inverse of the bump
    #   above, keyed the same way (subject, document, corpus) so that
    #   `bump(insert) then decrement(delete)` is a no-op — scoped to the
    #   deleted claim rows themselves (bounded by ONE file's claim count,
    #   never a corpus-wide scan). TCRD-296 gap #73: a full
    #   `rebuild_collection_stats` here used to re-derive the WHOLE
    #   collection's membership from `claims` on every single-document
    #   delete, which is exactly the cost this summary exists to avoid —
    #   on a collection with millions of claims, a facts pass re-extracting
    #   one document at a time turned every document into a full-collection
    #   regroup.
    # * `rebuild_collection_stats` — the RECOMPUTE (repair) path, called
    #   after any genuinely BULK claims mutation (corpus reassignment, the
    #   `full_documents` replace-mode delete, a fact merge/split, SharePoint
    #   collection consolidation) where computing a precise delta is not
    #   worth the complexity, plus the explicit admin repair tool
    #   (`agnes admin facts stats rebuild` / `POST /api/admin/facts/stats/
    #   rebuild`) and the one-time backfill an operator runs after this
    #   feature's migration, since the migration itself only creates the
    #   tables. Scoped to the affected collection(s) only — cheap even on a
    #   large instance, since it is bounded by ONE collection's own claims,
    #   never the whole graph — but still a full per-collection regroup, so
    #   it must never run once per document on a hot path; see
    #   `collection_stats_consistency_check` for a read-only way to verify
    #   the incremental legs above stay exact without invoking this repair.
    #
    # Every reader that consults these tables falls back to the original
    # claims-scan query, unchanged, whenever `fact_collection_stats` is
    # still empty (nothing has been rebuilt yet) — see `_visible_facts_for_
    # corpus_cte`/`_visible_edges_for_corpus_cte`/`approximate_counts_for_
    # collections`/`facet_top_values_for_collections` below, and
    # `_warn_collection_stats_fallback_once` for the one-time log line.
    # ------------------------------------------------------------------

    def _bump_collection_stats_on_new_claim(
        self,
        conn: Connection,
        *,
        fact_id: Optional[str],
        edge_id: Optional[str],
        corpus_id: str,
        corpus_file_id: str,
        new_claim_id: str,
    ) -> None:
        """Best-effort wrapper around :meth:`_bump_collection_stats_impl`:
        runs it under its OWN savepoint, so a failure there (the one
        expected case: `tests/db_pg/test_facts_read_pg.py`'s S9 backfill
        tests deliberately pin the Alembic chain to
        ``0083_ingest_runs_source_urls`` — before these tables existed —
        and seed data through this exact method, the same precedent the
        ``audience`` column's own docstring above already sets) rolls back
        ONLY the stats bookkeeping, never the claim `add_claim` just wrote.
        This summary is deliberately a best-effort accelerator, never a
        correctness-critical store — every reader falls back to the
        original `claims` scan when it is missing/stale (see
        `rebuild_collection_stats`'s docstring), so degrading silently here
        is the correct failure mode, not a swallowed bug."""
        # Gap #78 follow-up: bumped unconditionally, BEFORE the try below —
        # the claim itself is already written by the time this runs
        # (`add_claim` only calls this after a genuinely new row), so the
        # cache-invalidation signal must not depend on whether the stats
        # bookkeeping savepoint below happens to succeed.
        _bump_corpus_facts_version(corpus_id)
        try:
            with conn.begin_nested():
                self._bump_collection_stats_impl(
                    conn,
                    fact_id=fact_id,
                    edge_id=edge_id,
                    corpus_id=corpus_id,
                    corpus_file_id=corpus_file_id,
                    new_claim_id=new_claim_id,
                )
        except sa.exc.DBAPIError:
            _warn_collection_stats_unavailable_once()

    def _bump_collection_stats_impl(
        self,
        conn: Connection,
        *,
        fact_id: Optional[str],
        edge_id: Optional[str],
        corpus_id: str,
        corpus_file_id: str,
        new_claim_id: str,
    ) -> None:
        """Incrementally advance `fact_collection_membership` (or
        `edge_collection_membership`) and `fact_collection_stats` for ONE
        freshly-inserted claim. `conn` is the caller's own open transaction
        (`add_claim`'s atomicity contract) — this never opens its own.

        Every predicate below is scoped to ONE subject or ONE document
        (`:new_id` excludes the row `add_claim` just inserted, so "did this
        subject/document already have a claim" reads as it stood BEFORE
        this write) — no corpus-wide scan, so this is safe on the hot
        ingest path even for a collection with millions of claims."""
        is_edge = edge_id is not None
        subject_id = edge_id if is_edge else fact_id
        subject_col = "edge_id" if is_edge else "fact_id"

        doc_seen_for_subject = conn.execute(
            sa.text(
                f"SELECT EXISTS(SELECT 1 FROM claims WHERE {subject_col} = :sid "
                "AND corpus_file_id = :cfid AND id <> :new_id)"
            ),
            {"sid": subject_id, "cfid": corpus_file_id, "new_id": new_claim_id},
        ).scalar()
        doc_delta_for_subject = 0 if doc_seen_for_subject else 1

        if is_edge:
            was_new_subject = bool(
                conn.execute(
                    sa.text(
                        "INSERT INTO edge_collection_membership (corpus_id, edge_id, claims_count) "
                        "VALUES (:corpus_id, :sid, 1) "
                        "ON CONFLICT (corpus_id, edge_id) DO UPDATE SET "
                        "claims_count = edge_collection_membership.claims_count + 1 "
                        "RETURNING (xmax = 0)"
                    ),
                    {"corpus_id": corpus_id, "sid": subject_id},
                ).scalar()
            )
        else:
            was_new_subject = bool(
                conn.execute(
                    sa.text(
                        "INSERT INTO fact_collection_membership "
                        "(corpus_id, fact_id, claims_count, documents_count) "
                        "VALUES (:corpus_id, :sid, 1, :doc_delta) "
                        "ON CONFLICT (corpus_id, fact_id) DO UPDATE SET "
                        "claims_count = fact_collection_membership.claims_count + 1, "
                        "documents_count = fact_collection_membership.documents_count + :doc_delta "
                        "RETURNING (xmax = 0)"
                    ),
                    {"corpus_id": corpus_id, "sid": subject_id, "doc_delta": doc_delta_for_subject},
                ).scalar()
            )

        doc_seen_for_corpus = conn.execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM claims WHERE corpus_id = :corpus_id "
                "AND corpus_file_id = :cfid AND id <> :new_id)"
            ),
            {"corpus_id": corpus_id, "cfid": corpus_file_id, "new_id": new_claim_id},
        ).scalar()
        doc_delta_for_corpus = 0 if doc_seen_for_corpus else 1
        facts_delta = 1 if (was_new_subject and not is_edge) else 0
        edges_delta = 1 if (was_new_subject and is_edge) else 0

        conn.execute(
            sa.text(
                "INSERT INTO fact_collection_stats "
                "(corpus_id, facts_count, claims_count, edges_count, documents_with_claims, updated_at) "
                "VALUES (:corpus_id, :facts_delta, 1, :edges_delta, :doc_delta, now()) "
                "ON CONFLICT (corpus_id) DO UPDATE SET "
                "facts_count = fact_collection_stats.facts_count + :facts_delta, "
                "claims_count = fact_collection_stats.claims_count + 1, "
                "edges_count = fact_collection_stats.edges_count + :edges_delta, "
                "documents_with_claims = fact_collection_stats.documents_with_claims + :doc_delta, "
                "updated_at = now()"
            ),
            {
                "corpus_id": corpus_id,
                "facts_delta": facts_delta,
                "edges_delta": edges_delta,
                "doc_delta": doc_delta_for_corpus,
            },
        )

        if facts_delta:
            # TCRD-296 gap #81: this fact's FIRST claim in this collection —
            # the same "genuinely new" condition `facts_delta` itself already
            # gates on — so `fact_collection_type_counts` advances in lockstep
            # with `fact_collection_stats.facts_count`, never independently.
            fact_type = conn.execute(sa.text("SELECT type FROM facts WHERE id = :sid"), {"sid": subject_id}).scalar()
            if fact_type is not None:
                conn.execute(
                    sa.text(
                        "INSERT INTO fact_collection_type_counts (corpus_id, type, count) "
                        "VALUES (:corpus_id, :type, 1) "
                        "ON CONFLICT (corpus_id, type) DO UPDATE SET "
                        "count = fact_collection_type_counts.count + 1"
                    ),
                    {"corpus_id": corpus_id, "type": fact_type},
                )

    def _decrement_collection_stats_for_deleted_claims(
        self, conn: Connection, deleted_rows: Sequence[Mapping[str, Any]]
    ) -> None:
        """Best-effort wrapper around :meth:`_decrement_collection_stats_impl`:
        runs it under its OWN savepoint, so a failure there (the summary
        tables unreachable — an instance mid-migration, or a schema pinned
        before this feature, the same precedent `_bump_collection_stats_on_
        new_claim`'s docstring sets) rolls back ONLY the stats bookkeeping,
        never the claim delete `delete_claims_for_file` just committed. This
        summary is deliberately a best-effort accelerator, never a
        correctness-critical store — every reader falls back to the
        original `claims` scan when it is missing/stale (see
        `rebuild_collection_stats`'s docstring)."""
        if not deleted_rows:
            return
        # Gap #78 follow-up: same "bump before the savepoint, regardless of
        # its outcome" reasoning as `_bump_collection_stats_on_new_claim` —
        # the DELETE is already committed by the time this runs.
        _bump_corpus_facts_version(*{r["corpus_id"] for r in deleted_rows})
        try:
            with conn.begin_nested():
                self._decrement_collection_stats_impl(conn, deleted_rows)
        except sa.exc.DBAPIError:
            _warn_collection_stats_unavailable_once()

    def _decrement_collection_stats_impl(self, conn: Connection, deleted_rows: Sequence[Mapping[str, Any]]) -> None:
        """The exact inverse of :meth:`_bump_collection_stats_impl`, applied
        in bulk for every claim `delete_claims_for_file` just deleted for
        ONE file. `deleted_rows` (`corpus_id`, `fact_id`, `edge_id` per
        deleted claim, via `DELETE ... RETURNING`) bounds this to that one
        file's own claim count — never a corpus-wide scan, so this is safe
        to run inside the delete's own transaction even on a collection
        with millions of claims (TCRD-296 gap #73).

        A single `corpus_file_id` contributes to `documents_count`/
        `documents_with_claims` at most once per (corpus, subject) /
        (corpus,) pair — exactly the `id <> :new_id` check in the bump
        above establishes on insert. Deleting EVERY claim a file has for a
        subject therefore always removes that file's one contribution,
        regardless of how many other files still evidence the same
        subject — so every decrement below is unconditional on `deleted_
        rows`, never re-derived by rescanning `claims`.
        """
        fact_claims_removed: Dict[Tuple[str, str], int] = defaultdict(int)
        edge_claims_removed: Dict[Tuple[str, str], int] = defaultdict(int)
        corpus_claims_removed: Dict[str, int] = defaultdict(int)

        for row in deleted_rows:
            corpus_id = row["corpus_id"]
            corpus_claims_removed[corpus_id] += 1
            if row["fact_id"] is not None:
                fact_claims_removed[(corpus_id, row["fact_id"])] += 1
            if row["edge_id"] is not None:
                edge_claims_removed[(corpus_id, row["edge_id"])] += 1

        facts_removed_per_corpus: Dict[str, int] = defaultdict(int)
        edges_removed_per_corpus: Dict[str, int] = defaultdict(int)

        for (corpus_id, fact_id), removed in fact_claims_removed.items():
            remaining = conn.execute(
                sa.text(
                    "UPDATE fact_collection_membership SET "
                    "claims_count = claims_count - :removed, "
                    "documents_count = documents_count - 1 "
                    "WHERE corpus_id = :corpus_id AND fact_id = :fact_id "
                    "RETURNING claims_count"
                ),
                {"removed": removed, "corpus_id": corpus_id, "fact_id": fact_id},
            ).scalar()
            if remaining is None:
                continue  # summary not populated for this subject (fallback mode) — nothing to reconcile
            if remaining <= 0:
                conn.execute(
                    sa.text(
                        "DELETE FROM fact_collection_membership WHERE corpus_id = :corpus_id AND fact_id = :fact_id"
                    ),
                    {"corpus_id": corpus_id, "fact_id": fact_id},
                )
                facts_removed_per_corpus[corpus_id] += 1
                # TCRD-296 gap #81: this fact's LAST claim in this collection
                # just went away — the exact inverse condition the bump above
                # increments on — so decrement `fact_collection_type_counts`
                # too. `facts` itself is never touched by this delete path, so
                # the type lookup here always resolves.
                fact_type = conn.execute(sa.text("SELECT type FROM facts WHERE id = :fid"), {"fid": fact_id}).scalar()
                if fact_type is not None:
                    remaining_tc = conn.execute(
                        sa.text(
                            "UPDATE fact_collection_type_counts SET count = count - 1 "
                            "WHERE corpus_id = :corpus_id AND type = :type "
                            "RETURNING count"
                        ),
                        {"corpus_id": corpus_id, "type": fact_type},
                    ).scalar()
                    if remaining_tc is not None and remaining_tc <= 0:
                        conn.execute(
                            sa.text(
                                "DELETE FROM fact_collection_type_counts WHERE corpus_id = :corpus_id AND type = :type"
                            ),
                            {"corpus_id": corpus_id, "type": fact_type},
                        )

        for (corpus_id, edge_id), removed in edge_claims_removed.items():
            remaining = conn.execute(
                sa.text(
                    "UPDATE edge_collection_membership SET claims_count = claims_count - :removed "
                    "WHERE corpus_id = :corpus_id AND edge_id = :edge_id "
                    "RETURNING claims_count"
                ),
                {"removed": removed, "corpus_id": corpus_id, "edge_id": edge_id},
            ).scalar()
            if remaining is None:
                continue
            if remaining <= 0:
                conn.execute(
                    sa.text(
                        "DELETE FROM edge_collection_membership WHERE corpus_id = :corpus_id AND edge_id = :edge_id"
                    ),
                    {"corpus_id": corpus_id, "edge_id": edge_id},
                )
                edges_removed_per_corpus[corpus_id] += 1

        for corpus_id, claims_removed in corpus_claims_removed.items():
            remaining = conn.execute(
                sa.text(
                    "UPDATE fact_collection_stats SET "
                    "claims_count = claims_count - :claims_removed, "
                    "facts_count = facts_count - :facts_removed, "
                    "edges_count = edges_count - :edges_removed, "
                    "documents_with_claims = documents_with_claims - 1, "
                    "updated_at = now() "
                    "WHERE corpus_id = :corpus_id "
                    "RETURNING claims_count"
                ),
                {
                    "claims_removed": claims_removed,
                    "facts_removed": facts_removed_per_corpus.get(corpus_id, 0),
                    "edges_removed": edges_removed_per_corpus.get(corpus_id, 0),
                    "corpus_id": corpus_id,
                },
            ).scalar()
            if remaining is None:
                continue  # summary not populated for this collection (fallback mode) — nothing to reconcile
            if remaining <= 0:
                conn.execute(
                    sa.text("DELETE FROM fact_collection_stats WHERE corpus_id = :corpus_id"), {"corpus_id": corpus_id}
                )

    def collection_stats_consistency_check(self, corpus_id: str) -> Dict[str, Any]:
        """Read-only drift check: recompute one collection's stats straight
        from `claims` (the same ground-truth query `_rebuild_one_collection_
        stats` writes) and diff it against the currently MAINTAINED
        `fact_collection_membership`/`edge_collection_membership`/
        `fact_collection_stats`/`fact_collection_type_counts` rows, without
        writing anything.

        For tests (and an operator chasing a reported drift) to verify the
        incremental legs — `_bump_collection_stats_impl` on insert,
        `_decrement_collection_stats_impl` on delete — stay exact under
        normal operation. `rebuild_collection_stats` remains the actual
        repair tool; this helper never calls it and is not itself exposed
        as a new API surface.

        Returns ``{"consistent": bool, "maintained": {...}, "computed": {...}}``.
        """
        with self._engine.connect() as conn:
            maintained_stats_row = (
                conn.execute(
                    sa.text(
                        "SELECT facts_count, claims_count, edges_count, documents_with_claims "
                        "FROM fact_collection_stats WHERE corpus_id = :cid"
                    ),
                    {"cid": corpus_id},
                )
                .mappings()
                .first()
            )
            maintained_facts = {
                r["fact_id"]: (r["claims_count"], r["documents_count"])
                for r in conn.execute(
                    sa.text(
                        "SELECT fact_id, claims_count, documents_count "
                        "FROM fact_collection_membership WHERE corpus_id = :cid"
                    ),
                    {"cid": corpus_id},
                ).mappings()
            }
            maintained_edges = {
                r["edge_id"]: r["claims_count"]
                for r in conn.execute(
                    sa.text("SELECT edge_id, claims_count FROM edge_collection_membership WHERE corpus_id = :cid"),
                    {"cid": corpus_id},
                ).mappings()
            }
            maintained_type_counts = {
                r["type"]: r["count"]
                for r in conn.execute(
                    sa.text("SELECT type, count FROM fact_collection_type_counts WHERE corpus_id = :cid"),
                    {"cid": corpus_id},
                ).mappings()
            }

            computed_facts = {
                r["fact_id"]: (r["claims_count"], r["documents_count"])
                for r in conn.execute(
                    sa.text(
                        "SELECT fact_id, COUNT(*) AS claims_count, COUNT(DISTINCT corpus_file_id) AS documents_count "
                        "FROM claims WHERE corpus_id = :cid AND fact_id IS NOT NULL GROUP BY fact_id"
                    ),
                    {"cid": corpus_id},
                ).mappings()
            }
            computed_edges = {
                r["edge_id"]: r["claims_count"]
                for r in conn.execute(
                    sa.text(
                        "SELECT edge_id, COUNT(*) AS claims_count FROM claims "
                        "WHERE corpus_id = :cid AND edge_id IS NOT NULL GROUP BY edge_id"
                    ),
                    {"cid": corpus_id},
                ).mappings()
            }
            computed_type_counts = {
                r["type"]: r["n"]
                for r in conn.execute(
                    sa.text(
                        "SELECT f.type AS type, COUNT(DISTINCT c.fact_id) AS n FROM claims c "
                        "JOIN facts f ON f.id = c.fact_id "
                        "WHERE c.corpus_id = :cid AND c.fact_id IS NOT NULL GROUP BY f.type"
                    ),
                    {"cid": corpus_id},
                ).mappings()
            }
            computed_stats_row = (
                conn.execute(
                    sa.text(
                        "SELECT COUNT(DISTINCT fact_id) AS facts_count, COUNT(*) AS claims_count, "
                        "COUNT(DISTINCT edge_id) AS edges_count, COUNT(DISTINCT corpus_file_id) AS documents_with_claims "
                        "FROM claims WHERE corpus_id = :cid"
                    ),
                    {"cid": corpus_id},
                )
                .mappings()
                .first()
            )

        computed_stats = dict(computed_stats_row) if computed_stats_row and computed_stats_row["claims_count"] else None
        maintained = {
            "stats": dict(maintained_stats_row) if maintained_stats_row else None,
            "fact_membership": maintained_facts,
            "edge_membership": maintained_edges,
            "type_counts": maintained_type_counts,
        }
        computed = {
            "stats": computed_stats,
            "fact_membership": computed_facts,
            "edge_membership": computed_edges,
            "type_counts": computed_type_counts,
        }
        return {"consistent": maintained == computed, "maintained": maintained, "computed": computed}

    def rebuild_collection_stats(self, corpus_ids: Optional[List[str]] = None) -> Dict[str, int]:
        """Recompute `fact_collection_membership`/`edge_collection_membership`/
        `fact_collection_stats` from `claims` — the ground truth for "what is
        currently evidenced", as opposed to the incremental counters
        `_bump_collection_stats_on_new_claim` maintains on the ingest path.

        `corpus_ids=None` rebuilds EVERY collection that currently carries
        at least one claim, plus zeroes out (deletes) the summary row of any
        collection that no longer has one — a `full_documents` replace, a
        fact merge, or a SharePoint consolidation can empty a collection out
        entirely. Bounded PER COLLECTION (its own transaction, never one
        giant transaction spanning the whole graph) — safe to run on a live,
        multi-million-claim instance (TCRD-296 synthesis E.21): the migration
        that creates these tables does not backfill them (see
        `migrations/versions/0104_fact_collection_stats.py`), so this is also
        the one-time backfill an operator runs once after upgrading
        (`agnes admin facts stats rebuild` / `POST /api/admin/facts/stats/
        rebuild`), and the scoped recompute the delete/reassign/merge/split/
        consolidation call sites use to keep a handful of affected
        collections in sync after a bulk mutation.

        Returns ``{"collections_rebuilt": n}``."""
        if corpus_ids is None:
            with self._engine.connect() as conn:
                touched = conn.execute(sa.text("SELECT DISTINCT corpus_id FROM claims")).scalars().all()
                stale = conn.execute(sa.text("SELECT corpus_id FROM fact_collection_stats")).scalars().all()
            targets = sorted({*touched, *stale})
        else:
            targets = sorted(set(corpus_ids))

        for corpus_id in targets:
            with self._engine.begin() as conn:
                self._rebuild_one_collection_stats(conn, corpus_id)
        return {"collections_rebuilt": len(targets)}

    def _rebuild_one_collection_stats(self, conn: Connection, corpus_id: str) -> None:
        """The scoped recompute for ONE collection — see
        :meth:`rebuild_collection_stats`. `conn` is the caller's own open
        transaction.

        TCRD-296 gap #73b (live finding, 2026-09-04): a rebuild's own
        DELETE-then-INSERT is not safe against a concurrent WRITER of the
        same rows. Two guards, for two different concurrent writers:

        1. **A concurrent bump** (`_bump_collection_stats_impl`, still
           running on the ingest path for every OTHER collection's claims,
           and even this SAME collection's between this method's DELETE and
           its own INSERT) uses `INSERT ... ON CONFLICT DO UPDATE` — this
           method's own membership INSERTs below now use the identical
           `ON CONFLICT (corpus_id, fact_id|edge_id) DO UPDATE SET
           ... = EXCLUDED...` form so the two can never violate each
           other's primary key, whichever commits first.
        2. **A second concurrent rebuild of the SAME collection** — the
           `ON CONFLICT` above only makes ONE writer's INSERT safe against
           the OTHER's single-row bump; it does nothing to stop two
           rebuilds interleaving their own DELETE/INSERT pairs against each
           other (a plain DELETE has no ON CONFLICT to fall back on). The
           advisory lock below (`_COLLECTION_STATS_LOCK_CLASS_ID`) fully
           serializes that case instead — a second rebuild of this same
           `corpus_id` blocks until this transaction commits or rolls back.
        """
        # Gap #78 follow-up: a rebuild is the RECOMPUTE path every bulk
        # claims mutation (reassign, merge, split, consolidation) and the
        # admin repair tool route through — bumping here transitively
        # covers all of them without a separate hook at each call site.
        _bump_corpus_facts_version(corpus_id)
        conn.execute(
            sa.text("SELECT pg_advisory_xact_lock(:class_id, hashtext(:cid))"),
            {"class_id": _COLLECTION_STATS_LOCK_CLASS_ID, "cid": corpus_id},
        )
        conn.execute(sa.text("DELETE FROM fact_collection_membership WHERE corpus_id = :cid"), {"cid": corpus_id})
        conn.execute(sa.text("DELETE FROM edge_collection_membership WHERE corpus_id = :cid"), {"cid": corpus_id})
        conn.execute(
            sa.text(
                "INSERT INTO fact_collection_membership (corpus_id, fact_id, claims_count, documents_count) "
                "SELECT corpus_id, fact_id, COUNT(*), COUNT(DISTINCT corpus_file_id) FROM claims "
                "WHERE corpus_id = :cid AND fact_id IS NOT NULL GROUP BY corpus_id, fact_id "
                "ON CONFLICT (corpus_id, fact_id) DO UPDATE SET "
                "claims_count = EXCLUDED.claims_count, documents_count = EXCLUDED.documents_count"
            ),
            {"cid": corpus_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO edge_collection_membership (corpus_id, edge_id, claims_count) "
                "SELECT corpus_id, edge_id, COUNT(*) FROM claims "
                "WHERE corpus_id = :cid AND edge_id IS NOT NULL GROUP BY corpus_id, edge_id "
                "ON CONFLICT (corpus_id, edge_id) DO UPDATE SET claims_count = EXCLUDED.claims_count"
            ),
            {"cid": corpus_id},
        )
        # TCRD-296 gap #81: the per-type companion (``fact_collection_type_
        # counts``, migration 0110) — same DELETE-then-INSERT-with-ON-CONFLICT
        # shape as the membership tables above, for the same concurrent-writer
        # reasons. `COUNT(DISTINCT c.fact_id)` per type mirrors `facts_count`'s
        # own "one per distinct fact" semantics, just partitioned by type.
        conn.execute(
            sa.text("DELETE FROM fact_collection_type_counts WHERE corpus_id = :cid"),
            {"cid": corpus_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO fact_collection_type_counts (corpus_id, type, count) "
                "SELECT c.corpus_id, f.type, COUNT(DISTINCT c.fact_id) FROM claims c "
                "JOIN facts f ON f.id = c.fact_id "
                "WHERE c.corpus_id = :cid AND c.fact_id IS NOT NULL GROUP BY c.corpus_id, f.type "
                "ON CONFLICT (corpus_id, type) DO UPDATE SET count = EXCLUDED.count"
            ),
            {"cid": corpus_id},
        )
        stats = (
            conn.execute(
                sa.text(
                    "SELECT COUNT(DISTINCT fact_id) AS facts_count, COUNT(*) AS claims_count, "
                    "COUNT(DISTINCT edge_id) AS edges_count, COUNT(DISTINCT corpus_file_id) AS documents_with_claims "
                    "FROM claims WHERE corpus_id = :cid"
                ),
                {"cid": corpus_id},
            )
            .mappings()
            .first()
        )
        if stats and stats["claims_count"]:
            conn.execute(
                sa.text(
                    "INSERT INTO fact_collection_stats "
                    "(corpus_id, facts_count, claims_count, edges_count, documents_with_claims, updated_at) "
                    "VALUES (:cid, :facts_count, :claims_count, :edges_count, :documents_with_claims, now()) "
                    "ON CONFLICT (corpus_id) DO UPDATE SET "
                    "facts_count = EXCLUDED.facts_count, claims_count = EXCLUDED.claims_count, "
                    "edges_count = EXCLUDED.edges_count, "
                    "documents_with_claims = EXCLUDED.documents_with_claims, updated_at = now()"
                ),
                {"cid": corpus_id, **stats},
            )
        else:
            conn.execute(sa.text("DELETE FROM fact_collection_stats WHERE corpus_id = :cid"), {"cid": corpus_id})

    def delete_claims_for_file(self, corpus_file_id: str) -> int:
        """Delete every claim anchored to one ``corpus_files`` row; return the
        count.

        Used when the row's CONTENT is replaced in place (spec §6, "content
        changed"). Since #1655 an upload matching an existing row updates it
        in place instead of delete+insert, so the ``claims.corpus_file_id``
        ``ON DELETE CASCADE`` that used to clear a replaced document's claims
        no longer fires. Every claim carries a verbatim ``quote`` validated
        against the file's chunks at ingest (§8); once ``corpus_files.sha256``
        moves, those bytes are gone (the old blob is refcount-deleted) and the
        quote is unverifiable by construction — so the claims are dropped at
        replace time rather than served as current evidence until the
        producer's next extraction, which may never come for a hand-replaced
        file.

        Deliberately claims-only: facts/edges left with no evidence are
        removed by ``sweep_orphans`` as its own step afterwards (§6), exactly
        like the delete-driven cascade path.

        TCRD-296 gap #73: this is a HOT path, not a bulk one — a facts
        re-extraction pass calls it once per document, so a full
        ``rebuild_collection_stats`` here (this method's original
        implementation) turned every single-document delete into a
        full-collection regroup of ``claims``: on a collection with
        millions of claims, four concurrent extraction passes did that
        every few seconds, at ~230% Postgres CPU, for stats tables whose
        entire purpose is to make reads cheap. The collection-stats
        bookkeeping below is therefore an incremental delta — bounded by
        this one file's own claim count, never a corpus-wide scan — run
        INSIDE this delete's own transaction (unlike a genuinely bulk
        mutation, a delta this small costs nothing extra to keep atomic
        with the delete it reconciles).
        """
        with self._engine.begin() as conn:
            deleted_rows = (
                conn.execute(
                    sa.text("DELETE FROM claims WHERE corpus_file_id = :file_id RETURNING corpus_id, fact_id, edge_id"),
                    {"file_id": corpus_file_id},
                )
                .mappings()
                .all()
            )
            self._decrement_collection_stats_for_deleted_claims(conn, deleted_rows)
        return len(deleted_rows)

    def reassign_file_corpus(self, corpus_file_id: str, target_corpus_id: str) -> int:
        """Repoint one file's claims at the collection it now lives in; return
        the count.

        ``claims.corpus_id`` is denormalized from ``corpus_files`` on purpose —
        it is THE visibility predicate (an indexed equality filter, never a
        join through ``corpus_files`` on a traversal hop). Denormalized means
        it does not follow the file on its own, and nothing followed it: moving
        a file between collections updated ``corpus_files.corpus_id`` and left
        every claim pointing at the old one (Devin Review on #2068). Two
        consequences, and the visible one is the smaller: the graph facets
        grouped the file's facts under the collection it had left, and — since
        this column is what visibility is filtered on — its facts stayed
        readable to the OLD collection's audience and invisible to the new
        one's. Called on the move path, immediately after the file row moves.
        """
        with self._engine.begin() as conn:
            source_ids = (
                conn.execute(
                    sa.text("SELECT DISTINCT corpus_id FROM claims WHERE corpus_file_id = :file_id"),
                    {"file_id": corpus_file_id},
                )
                .scalars()
                .all()
            )
            result = conn.execute(
                sa.text("UPDATE claims SET corpus_id = :target WHERE corpus_file_id = :file_id"),
                {"target": target_corpus_id, "file_id": corpus_file_id},
            )
        # TCRD-296 E.21: both the vacated source collection(s) and the
        # target need a recompute — same reasoning as `delete_claims_
        # for_file`'s hook.
        affected = {*source_ids, target_corpus_id}
        if affected:
            self.rebuild_collection_stats(corpus_ids=list(affected))
        return int(result.rowcount or 0)

    def upsert_correction(
        self,
        *,
        subject_kind: str,
        subject_id: str,
        natural_keys: Any,
        verdict: str,
        reason: str,
        decided_by: str,
    ) -> None:
        if subject_kind not in ("fact", "edge"):
            raise ValueError(f"subject_kind must be 'fact' or 'edge', got {subject_kind!r}")
        if verdict not in ("wrong", "restricted", "revealed"):
            raise ValueError(f"verdict must be one of wrong/restricted/revealed, got {verdict!r}")
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO corrections "
                    "(subject_kind, subject_id, natural_keys, verdict, reason, decided_by) "
                    "VALUES (:kind, :id, CAST(:nk AS JSONB), :verdict, :reason, :by) "
                    "ON CONFLICT (subject_kind, subject_id) DO UPDATE SET "
                    "natural_keys = EXCLUDED.natural_keys, verdict = EXCLUDED.verdict, "
                    "reason = EXCLUDED.reason, decided_by = EXCLUDED.decided_by, decided_at = now()"
                ),
                {
                    "kind": subject_kind,
                    "id": subject_id,
                    "nk": json.dumps(natural_keys),
                    "verdict": verdict,
                    "reason": reason,
                    "by": decided_by,
                },
            )
        # Gap #78 follow-up: a correction changes VISIBILITY, never
        # claims/edges counts, so none of the three stats hooks above see
        # it — and it is scoped by subject id, not corpus, so the cheap
        # per-corpus bump those use is not available here. A blanket clear
        # is the correct, simple trade-off for this rare, admin-triggered
        # write (see `_clear_all_corpus_facts_versions`'s own docstring).
        _clear_all_corpus_facts_versions()

    def delete_correction(self, *, subject_kind: str, subject_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM corrections WHERE subject_kind = :kind AND subject_id = :id"),
                {"kind": subject_kind, "id": subject_id},
            )
        # Gap #78 follow-up: same reasoning as `upsert_correction` above.
        _clear_all_corpus_facts_versions()

    def list_wrong_corrections(self) -> List[Dict[str, Any]]:
        """The producer export (spec §7.4): every ``wrong`` subject with its
        ``natural_keys`` snapshot, so a re-extraction pass can prune them
        before re-asserting claims. Corrections are enforced at read time
        regardless (§4) -- this is a courtesy, not the enforcement boundary."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT subject_kind, subject_id, natural_keys, reason, decided_by, decided_at "
                        "FROM corrections WHERE verdict = 'wrong' ORDER BY decided_at"
                    )
                )
                .mappings()
                .all()
            )
        return [
            {
                "subject_kind": r["subject_kind"],
                "subject_id": r["subject_id"],
                "natural_keys": _decode_jsonb(r["natural_keys"]),
                "reason": r["reason"],
                "decided_by": r["decided_by"],
                "decided_at": r["decided_at"].isoformat() if r["decided_at"] else None,
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # shared visibility primitives (spec §5) — every read method below
    # goes through these, and only these.
    # ------------------------------------------------------------------

    @staticmethod
    def _visibility_predicate(column: str, is_admin: bool) -> str:
        """SQL fragment for "this claim is visible to the caller" — ``TRUE``
        for admin (no filter, per ``accessible_collection_ids``), else the
        outer reachability gate AND-ed with the audience selector (2026-08-30
        sharepoint-acl-mirroring plan, Task 10; spec §4.2-§4.4):

            outer gate (reachability):  corpus_id ∈ caller's readable set
            inner selector (variant):   audience ∈ caller's classes, or
                                         untagged outside a tiered-hidden
                                         collection

        The collection grant always wins on reachability — no audience tag
        makes a claim in an unreadable collection readable, the audience
        column only narrows WITHIN the readable set (never widens it). An
        untagged claim (``audience IS NULL``) stays unrestricted-within-
        collection, today's pre-audience behavior, UNLESS its collection is
        in ``:tiered_hidden`` (populated only under the ``must_not``
        guarantee mode, by :func:`_audience_context`) — enabling tiers on a
        scope is a gated re-index precisely so no untagged claim survives it
        there (spec §4.4). A tagged claim is visible iff its
        ``corpus_id || ':' || audience`` pair is in ``:audience_pairs``.

        ``column`` is always a literal we control (``c.corpus_id``), never
        caller input; the audience column referenced is threaded off the
        SAME alias (``column.rsplit(".", 1)[0]``) — every CTE/subquery this
        is used against must therefore select an ``audience`` column
        alongside whichever ``corpus_id`` it already selects. Every caller
        binding ``:readable`` when ``is_admin`` is False must now ALSO bind
        ``:tiered_hidden`` and ``:audience_pairs`` (see :func:`_audience_context`
        — empty lists are fine, but the params must be present or the bound
        SQL text fails to execute)."""
        if is_admin:
            return "TRUE"
        alias = column.rsplit(".", 1)[0]
        audience_column = f"{alias}.audience"
        return (
            f"({column} = ANY(:readable) AND ("
            f"({audience_column} IS NULL AND NOT ({column} = ANY(:tiered_hidden)))"
            f" OR (({column} || ':' || {audience_column}) = ANY(:audience_pairs))"
            "))"
        )

    @staticmethod
    def _alias_readable_sql(*, revealed_expr: str, is_admin: bool) -> str:
        """SQL fragment for "this ``fact_aliases fa`` row is showable to
        this caller" (security hardening — module docstring's alias-
        visibility rule): unconditional ``TRUE`` for admin, matching
        :meth:`_visibility_predicate`'s own god-mode short-circuit — an
        admin sees every alias whether or not ``fact_alias_sources`` has
        been backfilled for it, never gated on that table happening to
        have a row. For everyone else: ``revealed_expr`` (bypasses grants
        entirely, spec §4) OR at least one ``fact_alias_sources`` row for
        this EXACT ``(fa.type, fa.natural_key)`` whose ``corpus_id`` the
        caller can read. Every caller must alias ``fact_aliases`` as
        ``fa`` and bind ``:readable`` when ``is_admin`` is False.

        Deliberately audience-agnostic (Task 10, spec §4.4): a name's
        provenance is corpus-level (``fact_alias_sources.corpus_id`` — which
        COLLECTION's evidence minted this alias string), never claim-level,
        so there is no ``audience`` column to select here and nothing for
        :meth:`_visibility_predicate`'s inner selector to narrow. An alias
        stays visible whenever its provenance corpus is readable, exactly as
        before this task — same reasoning as ``_projection_cte_sql``'s
        alias-visibility rule, one layer up."""
        if is_admin:
            return "TRUE"
        return (
            f"({revealed_expr} OR EXISTS ("
            "SELECT 1 FROM fact_alias_sources s "
            "WHERE s.type = fa.type AND s.natural_key = fa.natural_key AND s.corpus_id = ANY(:readable)"
            "))"
        )

    def _subject_status(
        self,
        conn,
        *,
        subject_kind: str,
        subject_id: str,
        is_admin: bool,
        all_evidence: bool,
        readable: Optional[frozenset] = None,
        tiered_hidden: Optional[List[str]] = None,
        audience_pairs: Optional[List[str]] = None,
    ) -> Dict[str, bool]:
        """One query, reused by every single-subject visibility check
        (``neighbors`` node expansion, ``claims``) — same shape and cost
        whether ``subject_id`` exists or not, so a nonexistent id and a
        withheld/unreadable one cost the same (spec §5 rule 2).

        For ``subject_kind='fact'`` the existence corpus (``relevant_claims``
        below) is the subject's OWN claims UNION the claims of every
        incident edge that is not itself withheld (module docstring,
        "Endpoint evidence") — an endpoint-only fact (zero own claims) can
        therefore be visible purely because an edge naming it carries a
        readable claim. For ``subject_kind='edge'`` the corpus is unchanged:
        the edge's own claims only (spec S3 — edge visibility is never
        inferred from its endpoints).

        ``tiered_hidden``/``audience_pairs`` (Task 10, from
        :func:`_audience_context`) thread the audience selector into the
        SAME ``vis`` fragment below — ``relevant_claims`` therefore selects
        ``audience`` alongside ``corpus_id`` so :meth:`_visibility_predicate`
        has a column to read off the ``rc`` alias."""
        vis = self._visibility_predicate("rc.corpus_id", is_admin)
        if subject_kind == "fact":
            relevant_claims_cte = """
                relevant_claims AS (
                    SELECT corpus_id, audience FROM claims WHERE fact_id = :subject_id
                    UNION ALL
                    SELECT c.corpus_id, c.audience
                    FROM claims c
                    JOIN edges e ON e.id = c.edge_id
                    WHERE (e.src = :subject_id OR e.dst = :subject_id)
                      AND NOT EXISTS (
                        SELECT 1 FROM corrections co
                        WHERE co.subject_kind = 'edge' AND co.subject_id = e.id
                          AND co.verdict IN ('wrong', 'restricted')
                      )
                )
            """
        else:
            relevant_claims_cte = (
                "relevant_claims AS (SELECT corpus_id, audience FROM claims WHERE edge_id = :subject_id)"
            )
        if all_evidence:
            has_claim_visibility = (
                "EXISTS (SELECT 1 FROM relevant_claims) "
                f"AND NOT EXISTS (SELECT 1 FROM relevant_claims rc WHERE NOT ({vis}))"
            )
        else:
            has_claim_visibility = f"EXISTS (SELECT 1 FROM relevant_claims rc WHERE {vis})"
        sql = sa.text(
            f"WITH {relevant_claims_cte} "
            "SELECT "
            "EXISTS (SELECT 1 FROM corrections co WHERE co.subject_kind = :kind AND co.subject_id = :subject_id "
            "        AND co.verdict IN ('wrong', 'restricted')) AS withheld, "
            "EXISTS (SELECT 1 FROM corrections co WHERE co.subject_kind = :kind AND co.subject_id = :subject_id "
            "        AND co.verdict = 'revealed') AS revealed, "
            f"({has_claim_visibility}) AS has_claim_visibility"
        )
        params: Dict[str, Any] = {"kind": subject_kind, "subject_id": subject_id}
        if not is_admin:
            params["readable"] = list(readable) if readable else []
            params["tiered_hidden"] = tiered_hidden or []
            params["audience_pairs"] = audience_pairs or []
        row = conn.execute(sql, params).mappings().first()
        assert row is not None
        return dict(row)

    def _is_visible(self, status: Dict[str, bool]) -> bool:
        if status["withheld"]:
            return False
        return bool(status["revealed"] or status["has_claim_visibility"])

    def _fact_visible_sql(self, expr: str, *, is_admin: bool, all_evidence: bool) -> str:
        """SQL predicate for "the fact named by ``expr`` is visible to the
        caller" — the rule :meth:`_subject_status` + :meth:`_is_visible`
        apply one subject at a time (withheld -> never; otherwise
        ``revealed`` OR readable evidence among the fact's OWN claims ∪ the
        claims of a non-withheld incident edge, under the any/all_evidence
        mode), written as an inline predicate so :meth:`edges` can gate BOTH
        endpoints in SQL, before ``LIMIT`` (spec §5 rule 1 — a Python post-
        filter here would reopen the S6 shortfall oracle, and rule 3 — a
        listed edge must never reveal that it continues into a subject the
        caller may not see). ``expr`` is always a column reference we control
        (``e.src`` / ``e.dst``), never caller input. Callers bind
        ``:readable``, ``:tiered_hidden`` and ``:audience_pairs`` when
        ``is_admin`` is False."""
        vis = self._visibility_predicate("rc.corpus_id", is_admin)
        relevant = (
            "(SELECT rc0.corpus_id, rc0.audience FROM claims rc0 WHERE rc0.fact_id = {expr} "
            "UNION ALL "
            "SELECT rc1.corpus_id, rc1.audience FROM claims rc1 JOIN edges e1 ON e1.id = rc1.edge_id "
            "WHERE (e1.src = {expr} OR e1.dst = {expr}) AND NOT EXISTS ("
            "SELECT 1 FROM corrections co1 WHERE co1.subject_kind = 'edge' AND co1.subject_id = e1.id "
            "AND co1.verdict IN ('wrong', 'restricted')))"
        ).format(expr=expr)
        if all_evidence:
            has_vis = (
                f"(EXISTS (SELECT 1 FROM {relevant} rc) AND NOT EXISTS (SELECT 1 FROM {relevant} rc WHERE NOT ({vis})))"
            )
        else:
            has_vis = f"EXISTS (SELECT 1 FROM {relevant} rc WHERE {vis})"
        return (
            "(NOT EXISTS (SELECT 1 FROM corrections cw WHERE cw.subject_kind = 'fact' "
            f"AND cw.subject_id = {expr} AND cw.verdict IN ('wrong', 'restricted')) "
            "AND (EXISTS (SELECT 1 FROM corrections cr WHERE cr.subject_kind = 'fact' "
            f"AND cr.subject_id = {expr} AND cr.verdict = 'revealed') OR {has_vis}))"
        )

    def _subject_statuses(
        self,
        conn,
        *,
        subject_kind: str,
        ids: List[str],
        is_admin: bool,
        all_evidence: bool,
        readable: Optional[frozenset] = None,
        tiered_hidden: Optional[List[str]] = None,
        audience_pairs: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, bool]]:
        """Batched :meth:`_subject_status` — ONE query for a whole hop's
        worth of endpoints (TCRD-295: ``neighbors()`` used to run the single-
        subject form once per discovered node, two round trips per node on a
        hub). Same ``relevant_claims`` definition, same three flags per id,
        keyed by ``subject_id``; an id with no row (should not happen — every
        id yields a row via the ``ids`` CTE) reads as invisible."""
        if not ids:
            return {}
        vis = self._visibility_predicate("rc.corpus_id", is_admin)
        if subject_kind == "fact":
            relevant_claims_cte = """
                relevant_claims AS (
                    SELECT c.fact_id AS subject_id, c.corpus_id, c.audience
                    FROM claims c JOIN ids i ON i.subject_id = c.fact_id
                    UNION ALL
                    SELECT i.subject_id, c.corpus_id, c.audience
                    FROM ids i
                    JOIN edges e ON (e.src = i.subject_id OR e.dst = i.subject_id)
                    JOIN claims c ON c.edge_id = e.id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM corrections co
                        WHERE co.subject_kind = 'edge' AND co.subject_id = e.id
                          AND co.verdict IN ('wrong', 'restricted')
                    )
                )
            """
        else:
            relevant_claims_cte = (
                "relevant_claims AS (SELECT c.edge_id AS subject_id, c.corpus_id, c.audience "
                "FROM claims c JOIN ids i ON i.subject_id = c.edge_id)"
            )
        if all_evidence:
            has_claim_visibility = (
                "EXISTS (SELECT 1 FROM relevant_claims rc WHERE rc.subject_id = i.subject_id) "
                "AND NOT EXISTS (SELECT 1 FROM relevant_claims rc "
                f"WHERE rc.subject_id = i.subject_id AND NOT ({vis}))"
            )
        else:
            has_claim_visibility = (
                f"EXISTS (SELECT 1 FROM relevant_claims rc WHERE rc.subject_id = i.subject_id AND {vis})"
            )
        sql = sa.text(
            "WITH ids AS (SELECT unnest(CAST(:ids AS text[])) AS subject_id), "
            f"{relevant_claims_cte} "
            "SELECT i.subject_id, "
            "EXISTS (SELECT 1 FROM corrections co WHERE co.subject_kind = :kind AND co.subject_id = i.subject_id "
            "        AND co.verdict IN ('wrong', 'restricted')) AS withheld, "
            "EXISTS (SELECT 1 FROM corrections co WHERE co.subject_kind = :kind AND co.subject_id = i.subject_id "
            "        AND co.verdict = 'revealed') AS revealed, "
            f"({has_claim_visibility}) AS has_claim_visibility "
            "FROM ids i"
        )
        params: Dict[str, Any] = {"kind": subject_kind, "ids": list(ids)}
        if not is_admin:
            params["readable"] = list(readable) if readable else []
            params["tiered_hidden"] = tiered_hidden or []
            params["audience_pairs"] = audience_pairs or []
        rows = conn.execute(sql, params).mappings().all()
        return {r["subject_id"]: dict(r) for r in rows}

    @staticmethod
    def _projection_cte_sql(*, with_aliases: bool, is_admin: bool) -> str:
        """The per-key latest-document_date-wins attrs projection (spec
        §12), factored out of ``search()`` so ``neighbors()`` can serve
        the SAME projected shape on its nodes/edges without duplicating
        the ~40-line CTE chain. Consumes two CTEs the CALLER must already
        have defined earlier in the same ``WITH`` clause:

        - ``target_ids(subject_id, revealed)`` — the EXACT set of subjects
          to project (never a broader set — this is projection, not a
          visibility gate; the caller has already decided who is visible)
          plus each one's ``revealed``-correction status.
        - ``counted_claims(claim_id, subject_id, attrs, document_date)`` —
          the OWN, already caller-filtered claims to project from (own-
          claims-only, per S2: never the endpoint-evidence union).

        Produces ``subject_attrs(subject_id, attrs)``,
        ``counts(subject_id, claim_count)`` and, when ``with_aliases``,
        ``aliases(subject_id, aliases)`` (facts only — edges carry no
        aliases).

        **Alias visibility (security hardening, spec §5 extended to
        names).** A subject's OWN claims being readable does not make every
        one of its aliases readable — an alias can be minted from a
        DIFFERENT corpus than the one that made the subject visible at all
        (the exact shape of the bug this closes: one readable, unrelated
        claim plus one unreadable claim that named the subject). Each alias
        is therefore filtered to those with a ``fact_alias_sources`` row
        whose ``corpus_id`` the caller can read — same bypass as attrs: a
        ``revealed`` subject (``target_ids.revealed``) serves every alias
        it carries regardless of grants (spec §4)."""
        alias_readable = FactsPgRepository._alias_readable_sql(revealed_expr="t.revealed", is_admin=is_admin)
        parts = [
            """attr_kv AS (
                SELECT cc.subject_id, kv.key, kv.value, cc.document_date
                FROM counted_claims cc
                CROSS JOIN LATERAL jsonb_each(cc.attrs) AS kv(key, value)
            )""",
            """key_maxdate AS (
                SELECT subject_id, key, MAX(document_date) AS max_dated
                FROM attr_kv
                WHERE document_date IS NOT NULL
                GROUP BY subject_id, key
            )""",
            """winning AS (
                SELECT ak.subject_id, ak.key, ak.value, ak.document_date
                FROM attr_kv ak
                LEFT JOIN key_maxdate kmd ON kmd.subject_id = ak.subject_id AND kmd.key = ak.key
                WHERE (kmd.max_dated IS NOT NULL AND ak.document_date = kmd.max_dated)
                   OR (kmd.max_dated IS NULL)
            )""",
            """attr_proj AS (
                SELECT subject_id, key,
                       COUNT(DISTINCT value) AS n_distinct,
                       MAX(document_date) AS rep_date,
                       jsonb_agg(DISTINCT value) AS values_agg,
                       (array_agg(value))[1] AS single_value
                FROM winning
                GROUP BY subject_id, key
            )""",
            """attr_value_json AS (
                SELECT subject_id, key,
                    CASE WHEN n_distinct = 1
                        THEN jsonb_build_object('value', single_value, 'document_date', to_jsonb(rep_date))
                        ELSE jsonb_build_object('conflicted', true, 'values', values_agg)
                    END AS proj
                FROM attr_proj
            )""",
            """subject_attrs AS (
                SELECT subject_id, jsonb_object_agg(key, proj) AS attrs
                FROM attr_value_json
                GROUP BY subject_id
            )""",
        ]
        if with_aliases:
            parts.append(
                f"""aliases AS (
                SELECT fa.fact_id AS subject_id, jsonb_agg(fa.natural_key ORDER BY fa.natural_key) AS aliases
                FROM fact_aliases fa
                JOIN target_ids t ON t.subject_id = fa.fact_id
                WHERE {alias_readable}
                GROUP BY fa.fact_id
            )"""
            )
        parts.append(
            """counts AS (
                SELECT t.subject_id, COUNT(cc.claim_id) AS claim_count
                FROM target_ids t
                LEFT JOIN counted_claims cc ON cc.subject_id = t.subject_id
                GROUP BY t.subject_id
            )"""
        )
        return ",\n".join(parts)

    def _project_subjects(
        self,
        conn,
        *,
        kind: str,
        ids_with_revealed: List[tuple],
        is_admin: bool,
        readable: Optional[frozenset],
        with_aliases: bool,
        tiered_hidden: Optional[List[str]] = None,
        audience_pairs: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Own-claims-only attrs/aliases/claim_count projection for an
        EXACT, already-determined set of subject ids — never a visibility
        gate itself (the caller has already decided which ids to project;
        ``neighbors()`` calls this AFTER truncation, per spec §12's cost
        note, so the projection never runs on more nodes/edges than the
        capped result set). Shares :meth:`_projection_cte_sql` with
        ``search()``, so a neighbors node/edge carries byte-identical
        projected ``attrs`` to a search subject (spec §12). A subject
        carrying an active ``revealed`` correction projects from ALL its
        own claims regardless of grants (spec §4, matching ``search()``);
        every other subject projects only from claims the caller can
        read."""
        if not ids_with_revealed:
            return {}
        ids = [i for i, _r in ids_with_revealed]
        revealed_flags = [bool(r) for _i, r in ids_with_revealed]
        kind_column = "fact_id" if kind == "fact" else "edge_id"
        vis = self._visibility_predicate("c.corpus_id", is_admin)
        alias_select = ", COALESCE(al.aliases, '[]'::jsonb) AS aliases" if with_aliases else ""
        alias_join = "LEFT JOIN aliases al ON al.subject_id = t.subject_id" if with_aliases else ""
        sql = sa.text(
            f"""
            WITH target_ids AS (
                SELECT * FROM unnest(CAST(:ids AS text[]), CAST(:revealed AS bool[])) AS t(subject_id, revealed)
            ),
            counted_claims AS (
                SELECT c.id AS claim_id, c.{kind_column} AS subject_id, c.attrs, c.document_date
                FROM claims c
                JOIN target_ids t ON t.subject_id = c.{kind_column}
                WHERE t.revealed OR ({vis})
            ),
            {self._projection_cte_sql(with_aliases=with_aliases, is_admin=is_admin)}
            SELECT t.subject_id,
                   COALESCE(cnt.claim_count, 0) AS claim_count{alias_select},
                   COALESCE(satt.attrs, '{{}}'::jsonb) AS attrs
            FROM target_ids t
            LEFT JOIN counts cnt ON cnt.subject_id = t.subject_id
            {alias_join}
            LEFT JOIN subject_attrs satt ON satt.subject_id = t.subject_id
            """
        )
        params: Dict[str, Any] = {"ids": ids, "revealed": revealed_flags}
        if not is_admin:
            params["readable"] = list(readable) if readable else []
            params["tiered_hidden"] = tiered_hidden or []
            params["audience_pairs"] = audience_pairs or []
        rows = conn.execute(sql, params).mappings().all()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            out[r["subject_id"]] = {
                "aliases": (_decode_jsonb(r["aliases"]) or []) if with_aliases else [],
                "attrs": _decode_jsonb(r["attrs"]) or {},
                "claim_count": int(r["claim_count"]),
            }
        return out

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def search(
        self,
        caller,
        *,
        type: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        q: Optional[str] = None,
        limit: int = MAX_SEARCH_LIMIT,
        include_claims: int = 0,
    ) -> Dict[str, Any]:
        """Type/filter search over visible FACT subjects (spec §5). A
        subject's EXISTENCE gate is the endpoint-evidence union (module
        docstring): its own claims OR the claims of a non-withheld incident
        edge. ``attrs``, ``claim_count`` and ``quote_count`` stay OWN-claims
        ONLY (via ``counted_claims``/``attr_kv`` below, never
        ``endpoint_claims``) — an endpoint-only fact (visible purely via an
        edge) always serves ``attrs: {}`` and a ``claim_count`` of 0, closing
        the attribute oracle (S2) exactly as before this refinement.

        Audience variants (Task 10, spec §4.2): ``counted_claims`` filters
        through :meth:`_visibility_predicate`, so a claim whose audience tag
        the caller doesn't hold never enters ``attr_kv`` at all — the S2
        guarantee ("attrs never include an unreadable variant") extends to
        variants mechanically, no extra code here. Unlike ``claims()``, this
        method's rows are one-per-SUBJECT (already ``GROUP BY subject_id``
        through ``_projection_cte_sql``'s CTE chain), never one-per-CLAIM, so
        there is no ``(fact_id, corpus_file_id, edge_id)`` grouping key left
        to apply :func:`_pick_most_privileged` to by the time rows reach this
        method — a caller holding more than one audience class on the SAME
        tiered collection may see ``attrs`` degrade to ``"conflicted": true``
        for a key where distinct-privilege claim variants disagree, rather
        than the most-privileged value alone. Narrower than a leak (no
        variant outside the caller's classes is ever included); closing it
        would mean reworking the SQL projection to rank-filter claims BEFORE
        ``attr_kv``, left as a follow-up should multi-class membership on one
        tiered collection turn out to be a real shape.

        ``q`` is an OPTIONAL free-text name lookup, matched against
        ``fact_aliases.natural_key`` ONLY — never a claim's quote or attrs,
        so it can never reopen the S2 attribute oracle. It is a real FILTER
        (a fact without a matching alias never enters ``candidates`` at
        all — S6's shortfall rule: pre-limit, in SQL, never a Python
        post-filter), not merely a sort key. The query is normalized
        (casefolded, spaces -> hyphens) before matching so a natural-
        language name like "Parts Authority" matches the
        ``<type>:<kebab-slug>`` alias ``organization:parts-authority``.
        Matching is by substring, EXCEPT that a normalized ``q`` shorter
        than ``SHORT_Q_TOKEN_PREFIX_LENGTH`` must start a name TOKEN of the
        alias slug (the slug itself or a punctuation-delimited part of it:
        ``llr`` matches ``llr-corp`` and ``acme-llr``, never ``fullrange``)
        — a 2–3 character substring is a fragment of a large share of any
        real alias set and would fill the candidate cap with noise on every
        call. Matching subjects are RANKED — an exact match on the alias's
        slug (the part after the first ``:``) first, a slug prefix match
        second, any other match last, shorter alias and then ``subject_id``
        breaking further ties. No ``pg_trgm`` (or other extension)
        similarity ranking: this schema does not enable one, and this repo
        intentionally does not add the operational dependency — this
        deterministic CASE-based tiering needs nothing beyond stock
        Postgres. A blank/whitespace-only ``q`` degrades to "no filter"; a
        non-blank ``q`` shorter than ``MIN_SEARCH_Q_LENGTH`` raises
        ``ValueError`` (P2 review finding — a 1-char query has no useful
        selectivity against a full ILIKE scan).

        **Bounded candidate selection (live finding, 2026-09).** With ``q``
        the candidate set is selected as a ``MATERIALIZED`` CTE that is
        ranked by the rule above and cut at ``max(limit * 4,
        SEARCH_CANDIDATE_CAP)`` + 1 rows. This is the fix for the statement
        timeout the old shape hit on a large graph: matching via ``EXISTS
        (... fact_aliases ...)`` left the planner with no selectivity
        estimate for the ILIKE, it sized ``candidates`` at ~416k rows for an
        actual 122 and scanned all of ``claims`` and ``fact_aliases`` instead
        of probing two indexes 122 and 750 times (5.3 s vs 309 ms, same
        query, same instance). Because candidates are ORDERED by the same
        key the final result is, everything returned is still the best-
        ranked visible match; only when more readable aliases match than the
        cap admits can lower-ranked visible facts be missing, and that call
        carries the additive ``candidates_capped: true`` so the caller
        narrows ``q`` (or adds ``type``, which is applied BEFORE the cap)
        rather than reading the page as complete. Visibility and the
        attribute ``filters`` are evaluated on the capped set — deliberately,
        the visibility gate over ``claims`` is the join that exploded — so a
        capped page can be shorter than ``limit`` or empty while matches
        exist beyond the cap; that is exactly the case the flag names, and
        ``filters`` alone can never widen it. Both the
        match and the cap count READABLE (or revealed) aliases only — the
        same restriction S9 puts on the match itself — so neither can leak
        how many restricted names match. ``limit_applied`` keeps its own
        meaning (the caller's OWN visible set exceeds ``limit``). Without
        ``q`` the candidate set is the type filter alone, unbounded as
        before: the planner has real statistics for ``facts.type``, and a
        cap there would silently shorten a restricted caller's id-ordered
        page.

        The statement runs under a bounded Postgres statement timeout, same
        mechanism as ``neighbors()``; when it fires the caller gets
        :class:`FactsQueryTimeout` — whose message is the next step, never
        the raw driver text.

        ``include_claims=k`` (TCRD-295, ≤ ``MAX_INLINE_CLAIMS_K``) attaches
        the k newest readable claims to each returned subject via
        :meth:`_inline_claims` — same rules as :meth:`claims`, under the
        response budget — and adds ``claims_truncated`` to the response;
        both are absent at the default of 0, so the response shape is
        unchanged for existing callers."""
        filters = filters or {}
        if len(filters) > MAX_SEARCH_FILTERS:
            raise ValueError(f"too many filters (max {MAX_SEARCH_FILTERS})")
        limit = max(1, min(limit, MAX_SEARCH_LIMIT))
        include_claims = max(0, min(include_claims, MAX_INLINE_CLAIMS_K))

        readable = _readable_ids(caller)
        is_admin = readable is None
        all_evidence = _visibility_mode() == "all_evidence"
        tiered_hidden, audience_pairs = _audience_context(caller, readable)
        vis = self._visibility_predicate("c.corpus_id", is_admin)
        vis_ec = self._visibility_predicate("ec.corpus_id", is_admin)
        # `q` matching itself must never become an oracle for a restricted
        # name's existence (security hardening): only an alias the caller
        # can see (or a revealed subject, which bypasses grants entirely per
        # spec §4) counts as a match — AND as a candidate toward the cap.
        alias_readable = self._alias_readable_sql(
            revealed_expr="fa.fact_id IN (SELECT subject_id FROM revealed_ids)", is_admin=is_admin
        )

        q_clean = (q or "").strip()
        if q_clean and len(q_clean) < MIN_SEARCH_Q_LENGTH:
            raise ValueError(f"q must be at least {MIN_SEARCH_Q_LENGTH} characters")
        q_norm = q_clean.casefold().replace(" ", "-") if q_clean else None

        filter_clauses = []
        params: Dict[str, Any] = {"type": type, "limit_plus_one": limit + 1}
        if not is_admin:
            params["readable"] = list(readable)
            params["tiered_hidden"] = tiered_hidden
            params["audience_pairs"] = audience_pairs
        for i, (fkey, fval) in enumerate(filters.items()):
            params[f"fkey{i}"] = str(fkey)
            params[f"fval{i}"] = json.dumps(fval)
            filter_clauses.append(
                f"EXISTS (SELECT 1 FROM attr_proj ap WHERE ap.subject_id = v.subject_id "
                f"AND ap.key = :fkey{i} AND ap.n_distinct = 1 AND ap.single_value = CAST(:fval{i} AS JSONB))"
            )
        filter_sql = ("AND " + " AND ".join(filter_clauses)) if filter_clauses else ""

        type_sql = "(CAST(:type AS TEXT) IS NULL OR f.type = :type)"
        withheld_sql = """NOT EXISTS (
                    SELECT 1 FROM corrections co
                    WHERE co.subject_kind = 'fact' AND co.subject_id = f.id
                      AND co.verdict IN ('wrong', 'restricted')
                  )"""
        candidate_cap = max(limit * 4, SEARCH_CANDIDATE_CAP)
        if q_norm is not None:
            # ESCAPE '\' per the security playbook (F-series LIKE guidance):
            # a literal '%'/'_' in the query must never act as a wildcard.
            q_escaped = q_norm.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params["q_norm"] = q_norm
            params["q_prefix"] = f"{q_escaped}%"
            params["candidate_cap_plus_one"] = candidate_cap + 1
            if len(q_norm) < SHORT_Q_TOKEN_PREFIX_LENGTH:
                # Token-start rule for very short queries (see the constant):
                # a fixed boundary class + an escaped literal, matched
                # against the slug only so `org` cannot match every
                # `organization:` alias at position 0. Linear-time.
                params["q_token_re"] = "(^|[^[:alnum:]])" + _regex_literal(q_norm)
                alias_match_sql = "split_part(fa.natural_key, ':', 2) ~* :q_token_re"
            else:
                params["q_substr"] = f"%{q_escaped}%"
                alias_match_sql = "fa.natural_key ILIKE :q_substr ESCAPE '\\'"
            candidates_sql = f"""
            candidates AS MATERIALIZED (
                -- Ranked + capped: the planner's hard cardinality ceiling
                -- (docstring, "Bounded candidate selection"). Deterministic,
                -- extension-free tiers (no pg_trgm in this schema): exact
                -- slug match (0) < slug prefix match (1) < any other match
                -- (2), shortest alias next, id last. Ranking only over
                -- READABLE aliases — ranking by a restricted alias's match
                -- strength would leak its existence through result ORDER.
                SELECT f.id AS subject_id, f.type AS subject_type, m.match_tier, m.alias_len
                FROM (
                    SELECT fa.fact_id,
                           MIN(CASE
                                 WHEN split_part(fa.natural_key, ':', 2) = :q_norm THEN 0
                                 WHEN split_part(fa.natural_key, ':', 2) ILIKE :q_prefix ESCAPE '\\' THEN 1
                                 ELSE 2
                               END) AS match_tier,
                           MIN(LENGTH(fa.natural_key)) AS alias_len
                    FROM fact_aliases fa
                    WHERE {alias_match_sql}
                      AND {alias_readable}
                    GROUP BY fa.fact_id
                ) m
                JOIN facts f ON f.id = m.fact_id
                WHERE {type_sql}
                  AND {withheld_sql}
                ORDER BY m.match_tier, m.alias_len, f.id
                LIMIT :candidate_cap_plus_one
            )"""
        else:
            candidates_sql = f"""
            candidates AS MATERIALIZED (
                SELECT f.id AS subject_id, f.type AS subject_type,
                       CAST(NULL AS INTEGER) AS match_tier, CAST(NULL AS INTEGER) AS alias_len
                FROM facts f
                WHERE {type_sql}
                  AND {withheld_sql}
            )"""

        sql = sa.text(
            f"""
            WITH revealed_ids AS (
                SELECT subject_id FROM corrections WHERE subject_kind = 'fact' AND verdict = 'revealed'
            ),
            {candidates_sql},
            counted_claims AS (
                SELECT c.id AS claim_id, c.fact_id AS subject_id, c.attrs, c.document_date
                FROM claims c
                JOIN candidates cand ON cand.subject_id = c.fact_id
                WHERE cand.subject_id IN (SELECT subject_id FROM revealed_ids) OR ({vis})
            ),
            endpoint_claims AS (
                -- Existence-only: claims of edges incident to a candidate,
                -- via a non-withheld edge (module docstring, "Endpoint
                -- evidence"). Never joined into attr_kv/counted_claims — the
                -- attrs projection and claim_count stay OWN-claims-only.
                -- `audience` selected so `vis_ec` (Task 10's audience
                -- selector) has a column to read off this alias too — an
                -- endpoint claim that fails the audience gate doesn't count
                -- toward existence either.
                SELECT DISTINCT cand.subject_id AS subject_id, c.corpus_id AS corpus_id, c.audience AS audience
                FROM candidates cand
                JOIN edges e ON (e.src = cand.subject_id OR e.dst = cand.subject_id)
                JOIN claims c ON c.edge_id = e.id
                WHERE NOT EXISTS (
                    SELECT 1 FROM corrections co
                    WHERE co.subject_kind = 'edge' AND co.subject_id = e.id
                      AND co.verdict IN ('wrong', 'restricted')
                )
            ),
            visible AS (
                SELECT cand.subject_id, cand.subject_type, cand.match_tier, cand.alias_len,
                       (cand.subject_id IN (SELECT subject_id FROM revealed_ids)) AS is_revealed
                FROM candidates cand
                WHERE cand.subject_id IN (SELECT subject_id FROM revealed_ids)
                   OR (
                        NOT {str(all_evidence).upper()}
                        AND (
                            EXISTS (SELECT 1 FROM counted_claims cc WHERE cc.subject_id = cand.subject_id)
                            OR EXISTS (SELECT 1 FROM endpoint_claims ec WHERE ec.subject_id = cand.subject_id AND {vis_ec})
                        )
                   )
                   OR (
                        {str(all_evidence).upper()}
                        AND (
                            EXISTS (SELECT 1 FROM claims c2 WHERE c2.fact_id = cand.subject_id)
                            OR EXISTS (SELECT 1 FROM endpoint_claims ec WHERE ec.subject_id = cand.subject_id)
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM claims c3
                            WHERE c3.fact_id = cand.subject_id AND NOT ({self._visibility_predicate("c3.corpus_id", is_admin)})
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM endpoint_claims ec WHERE ec.subject_id = cand.subject_id AND NOT ({vis_ec})
                        )
                   )
            ),
            target_ids AS (
                SELECT subject_id, is_revealed AS revealed FROM visible
            ),
            {self._projection_cte_sql(with_aliases=True, is_admin=is_admin)},
            results AS (
                SELECT v.subject_id, v.subject_type, v.is_revealed, v.match_tier, v.alias_len,
                       COALESCE(cnt.claim_count, 0) AS claim_count,
                       COALESCE(al.aliases, '[]'::jsonb) AS aliases,
                       COALESCE(sa.attrs, '{{}}'::jsonb) AS attrs
                FROM visible v
                LEFT JOIN counts cnt ON cnt.subject_id = v.subject_id
                LEFT JOIN aliases al ON al.subject_id = v.subject_id
                LEFT JOIN subject_attrs sa ON sa.subject_id = v.subject_id
                WHERE TRUE {filter_sql}
                ORDER BY COALESCE(v.match_tier, 3), COALESCE(v.alias_len, 0), v.subject_id
                LIMIT :limit_plus_one
            )
            -- The aggregate always yields exactly one row, so the candidate
            -- count reaches Python even when `results` is empty (a capped
            -- selection none of whose members passed visibility/filters is
            -- still a capped selection the caller must be told about).
            SELECT r.subject_id, r.subject_type, r.is_revealed, r.claim_count, r.aliases, r.attrs,
                   c.n_candidates
            FROM (SELECT COUNT(*) AS n_candidates FROM candidates) c
            LEFT JOIN results r ON TRUE
            ORDER BY COALESCE(r.match_tier, 3), COALESCE(r.alias_len, 0), r.subject_id
            """
        )
        # P2 review finding: bound this statement the same way `neighbors()`
        # is bounded (spec §12) — an ILIKE-driven candidate scan with no cap
        # could stall a connection out of the pool. `.begin()` (not
        # `.connect()`) so `SET LOCAL` applies to the query that follows in
        # the same transaction.
        timeout_ms = _STATEMENT_TIMEOUT_MS
        try:
            with self._engine.begin() as conn:
                conn.execute(sa.text(f"SET LOCAL statement_timeout = {timeout_ms}"))
                raw_rows = conn.execute(sql, params).mappings().all()

                n_candidates = int(raw_rows[0]["n_candidates"]) if raw_rows else 0
                rows = [r for r in raw_rows if r["subject_id"] is not None]
                limit_applied = len(rows) > limit
                rows = rows[:limit]
                inline: Dict[str, List[Dict[str, Any]]] = {}
                claims_truncated = False
                if include_claims > 0:
                    inline, claims_truncated = self._inline_claims(
                        conn,
                        kind="fact",
                        ids_with_revealed=[(r["subject_id"], bool(r["is_revealed"])) for r in rows],
                        k=include_claims,
                        is_admin=is_admin,
                        readable=readable,
                        tiered_hidden=tiered_hidden,
                        audience_pairs=audience_pairs,
                    )
        except sa.exc.DBAPIError as exc:
            if _is_statement_timeout(exc):
                raise FactsQueryTimeout(_search_timeout_message(timeout_ms)) from exc
            raise

        subjects = []
        for r in rows:
            claim_count = int(r["claim_count"])
            is_revealed = bool(r["is_revealed"])
            subject: Dict[str, Any] = {
                "id": r["subject_id"],
                "type": r["subject_type"],
                "aliases": _decode_jsonb(r["aliases"]) or [],
                "attrs": _decode_jsonb(r["attrs"]) or {},
                "claim_count": claim_count,
                "quote_count": 0 if is_revealed else claim_count,
                "revealed": is_revealed,
            }
            if include_claims > 0 and r["subject_id"] in inline:
                subject["claims"] = inline[r["subject_id"]]
            subjects.append(subject)
        result: Dict[str, Any] = {"subjects": subjects, "limit_applied": limit_applied}
        if q_norm is not None and n_candidates > candidate_cap:
            # Additive, same convention as `collections_search`/knowledge
            # search: present only when true, so the default shape is
            # unchanged for existing callers.
            result["candidates_capped"] = True
        if include_claims > 0:
            result["claims_truncated"] = claims_truncated
        return result

    # ------------------------------------------------------------------
    # neighbors
    # ------------------------------------------------------------------

    def neighbors(
        self,
        caller,
        subject_id: str,
        *,
        edge_types: Optional[List[str]] = None,
        depth: int = 1,
        fanout: int = MAX_NEIGHBORS_FANOUT,
        limit: int = MAX_NEIGHBORS_RESULT,
        include_claims: int = 0,
    ) -> Dict[str, Any]:
        """Bounded traversal from one visible subject (spec §5 rule 3, §12).

        Per hop the discovered endpoints are checked in ONE batched status
        query and ONE type lookup (:meth:`_subject_statuses` — TCRD-295;
        this used to be two round trips per discovered node, the N+1 that
        made a hub walk cost 54 s of steps), with the exact same per-node
        visibility rule and the same result ordering as before: frontier
        nodes in sorted order, edges by id, fanout per node, the result cap
        applied as items are admitted.

        ``include_claims=k`` (≤ ``MAX_INLINE_CLAIMS_K``) attaches the k
        newest readable claims to each EDGE in the response via
        :meth:`_inline_claims` (nodes keep ``claim_count``), under the
        response budget; ``truncated["claims"]`` is present only when
        requested, so the default response shape is unchanged."""
        depth = max(1, min(depth, MAX_NEIGHBORS_DEPTH))
        fanout = max(1, min(fanout, MAX_NEIGHBORS_FANOUT))
        limit = max(1, min(limit, MAX_NEIGHBORS_RESULT))
        include_claims = max(0, min(include_claims, MAX_INLINE_CLAIMS_K))

        readable = _readable_ids(caller)
        is_admin = readable is None
        all_evidence = _visibility_mode() == "all_evidence"
        tiered_hidden, audience_pairs = _audience_context(caller, readable)

        with self._engine.begin() as conn:
            conn.execute(sa.text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))

            root_status = self._subject_status(
                conn,
                subject_kind="fact",
                subject_id=subject_id,
                is_admin=is_admin,
                all_evidence=all_evidence,
                readable=readable,
                tiered_hidden=tiered_hidden,
                audience_pairs=audience_pairs,
            )
            if not self._is_visible(root_status):
                raise FactNotFound(subject_id)

            root_row = (
                conn.execute(sa.text("SELECT id, type FROM facts WHERE id = :id"), {"id": subject_id})
                .mappings()
                .first()
            )
            if root_row is None:
                raise FactNotFound(subject_id)

            nodes: Dict[str, Dict[str, Any]] = {
                subject_id: {"id": root_row["id"], "type": root_row["type"], "revealed": bool(root_status["revealed"])}
            }
            edges_out: List[Dict[str, Any]] = []
            edges_seen: set = set()
            edges_revealed: Dict[str, bool] = {}
            visited = {subject_id}
            frontier = {subject_id}
            truncated: Dict[str, bool] = {"depth": False, "fanout": False, "result": False}

            vis = self._visibility_predicate("c.corpus_id", is_admin)
            edge_types_clause = "AND e.type = ANY(:edge_types)" if edge_types else ""
            edge_sql = sa.text(
                f"""
                SELECT e.id, e.src, e.dst, e.type,
                       EXISTS (
                         SELECT 1 FROM corrections co2 WHERE co2.subject_kind = 'edge'
                           AND co2.subject_id = e.id AND co2.verdict = 'revealed'
                       ) AS revealed
                FROM edges e
                WHERE (e.src = :node_id OR e.dst = :node_id)
                  {edge_types_clause}
                  AND NOT EXISTS (
                    SELECT 1 FROM corrections co WHERE co.subject_kind = 'edge' AND co.subject_id = e.id
                      AND co.verdict IN ('wrong', 'restricted')
                  )
                  AND (
                    EXISTS (
                      SELECT 1 FROM corrections co WHERE co.subject_kind = 'edge' AND co.subject_id = e.id
                        AND co.verdict = 'revealed'
                    )
                    OR EXISTS (SELECT 1 FROM claims c WHERE c.edge_id = e.id AND {vis})
                  )
                ORDER BY e.id
                LIMIT :fanout_plus_one
                """
            )

            for _hop in range(depth):
                if not frontier:
                    break
                if len(nodes) + len(edges_out) >= limit:
                    truncated["result"] = True
                    break

                # Phase 1 — this hop's edge pages, one per frontier node
                # (fanout is per node, so the query stays per node).
                hop_rows: List[Tuple[str, List[Any]]] = []
                for node_id in sorted(frontier):
                    params: Dict[str, Any] = {"node_id": node_id, "fanout_plus_one": fanout + 1}
                    if not is_admin:
                        params["readable"] = list(readable)
                        params["tiered_hidden"] = tiered_hidden
                        params["audience_pairs"] = audience_pairs
                    if edge_types:
                        params["edge_types"] = list(edge_types)
                    edge_rows = conn.execute(edge_sql, params).mappings().all()
                    if len(edge_rows) > fanout:
                        truncated["fanout"] = True
                        edge_rows = edge_rows[:fanout]
                    hop_rows.append((node_id, edge_rows))

                # Phase 2 — ONE status query + ONE type lookup for every
                # endpoint this hop could admit (the batched N+1 fix).
                candidate_ids: List[str] = []
                for node_id, edge_rows in hop_rows:
                    for erow in edge_rows:
                        other_id = erow["dst"] if erow["src"] == node_id else erow["src"]
                        if other_id not in nodes and other_id not in candidate_ids:
                            candidate_ids.append(other_id)
                statuses = self._subject_statuses(
                    conn,
                    subject_kind="fact",
                    ids=candidate_ids,
                    is_admin=is_admin,
                    all_evidence=all_evidence,
                    readable=readable,
                    tiered_hidden=tiered_hidden,
                    audience_pairs=audience_pairs,
                )
                types: Dict[str, str] = {}
                if candidate_ids:
                    types = {
                        r["id"]: r["type"]
                        for r in conn.execute(
                            sa.text("SELECT id, type FROM facts WHERE id = ANY(:ids)"), {"ids": candidate_ids}
                        ).mappings()
                    }

                # Phase 3 — admit in the exact order the per-node walk did.
                next_frontier: set = set()
                for node_id, edge_rows in hop_rows:
                    if len(nodes) + len(edges_out) >= limit:
                        truncated["result"] = True
                        break
                    for erow in edge_rows:
                        other_id = erow["dst"] if erow["src"] == node_id else erow["src"]
                        if other_id not in nodes:
                            other_status = statuses.get(other_id)
                            if other_status is None or not self._is_visible(other_status):
                                # traversal does not tunnel (§5 rule 3, S4):
                                # the endpoint is invisible -> skip the edge
                                # entirely, never reveal the path continues.
                                continue
                            other_type = types.get(other_id)
                            if other_type is None:
                                continue
                            nodes[other_id] = {
                                "id": other_id,
                                "type": other_type,
                                "revealed": bool(other_status["revealed"]),
                            }
                        if erow["id"] not in edges_seen:
                            edges_seen.add(erow["id"])
                            edges_revealed[erow["id"]] = bool(erow["revealed"])
                            edges_out.append(
                                {"id": erow["id"], "src": erow["src"], "dst": erow["dst"], "type": erow["type"]}
                            )
                        if other_id not in visited:
                            next_frontier.add(other_id)
                        if len(nodes) + len(edges_out) >= limit:
                            truncated["result"] = True
                            break
                    if len(nodes) + len(edges_out) >= limit:
                        break
                visited |= next_frontier
                frontier = next_frontier
            else:
                # The loop ran all `depth` hops without an early `break` —
                # only then is "we stopped because depth ran out" even a
                # candidate explanation. Check whether the final frontier
                # actually has any further visible edge before flagging
                # truncation: merely discovering nodes at the last hop does
                # NOT mean the graph continues past them (a leaf reached
                # exactly at the depth ceiling must not be reported as cut
                # off).
                if frontier and not truncated["result"]:
                    seen_clause = "AND e.id != ALL(:seen) " if edges_seen else ""
                    check_sql = sa.text(
                        f"""
                        SELECT 1 FROM edges e
                        WHERE (e.src = ANY(:ids) OR e.dst = ANY(:ids))
                          {seen_clause}
                          AND NOT EXISTS (
                            SELECT 1 FROM corrections co WHERE co.subject_kind = 'edge' AND co.subject_id = e.id
                              AND co.verdict IN ('wrong', 'restricted')
                          )
                          AND (
                            EXISTS (
                              SELECT 1 FROM corrections co WHERE co.subject_kind = 'edge' AND co.subject_id = e.id
                                AND co.verdict = 'revealed'
                            )
                            OR EXISTS (SELECT 1 FROM claims c WHERE c.edge_id = e.id AND {vis})
                          )
                        LIMIT 1
                        """
                    )
                    check_params: Dict[str, Any] = {"ids": list(frontier)}
                    if edges_seen:
                        check_params["seen"] = list(edges_seen)
                    if not is_admin:
                        check_params["readable"] = list(readable)
                        check_params["tiered_hidden"] = tiered_hidden
                        check_params["audience_pairs"] = audience_pairs
                    more = conn.execute(check_sql, check_params).first()
                    truncated["depth"] = more is not None

            # Project attrs/aliases/claim_count for the FINAL, already-
            # truncated node/edge sets only (spec §12 cost note: "project
            # AFTER truncation") — never the wider candidate set a deeper
            # walk might have touched. Same shared projection `search()`
            # uses, so a node/edge here carries byte-identical `attrs` to
            # a `search()` subject (spec §12).
            node_proj = self._project_subjects(
                conn,
                kind="fact",
                ids_with_revealed=[(n["id"], n["revealed"]) for n in nodes.values()],
                is_admin=is_admin,
                readable=readable,
                with_aliases=True,
                tiered_hidden=tiered_hidden,
                audience_pairs=audience_pairs,
            )
            edges_with_revealed = [(eid, edges_revealed.get(eid, False)) for eid in edges_seen]
            edge_proj = self._project_subjects(
                conn,
                kind="edge",
                ids_with_revealed=edges_with_revealed,
                is_admin=is_admin,
                readable=readable,
                with_aliases=False,
                tiered_hidden=tiered_hidden,
                audience_pairs=audience_pairs,
            )
            inline: Dict[str, List[Dict[str, Any]]] = {}
            if include_claims > 0:
                inline, truncated["claims"] = self._inline_claims(
                    conn,
                    kind="edge",
                    ids_with_revealed=[(e["id"], edges_revealed.get(e["id"], False)) for e in edges_out],
                    k=include_claims,
                    is_admin=is_admin,
                    readable=readable,
                    tiered_hidden=tiered_hidden,
                    audience_pairs=audience_pairs,
                )

        nodes_out = []
        for n in nodes.values():
            proj = node_proj.get(n["id"], {"aliases": [], "attrs": {}, "claim_count": 0})
            claim_count = proj["claim_count"]
            nodes_out.append(
                {
                    "id": n["id"],
                    "type": n["type"],
                    "aliases": proj["aliases"],
                    "attrs": proj["attrs"],
                    "claim_count": claim_count,
                    "quote_count": 0 if n["revealed"] else claim_count,
                    "revealed": n["revealed"],
                }
            )
        for e in edges_out:
            e["attrs"] = edge_proj.get(e["id"], {"attrs": {}})["attrs"]
            if include_claims > 0 and e["id"] in inline:
                e["claims"] = inline[e["id"]]

        return {
            "nodes": nodes_out,
            "edges": edges_out,
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    # claims
    # ------------------------------------------------------------------

    def claims(self, caller, subject_id: str, *, limit: int = MAX_CLAIMS_LIMIT) -> Dict[str, Any]:
        """List a subject's OWN claims, newest first, capped at ``limit``
        (``MAX_CLAIMS_LIMIT`` ceiling). The visibility GATE (below, via
        `_subject_status`) uses the endpoint-evidence union for a fact
        subject, so a visible endpoint-only fact (zero own claims, visible
        only via an incident edge's claim) returns `{"claims": [], ...}`
        (200) rather than a 404 — the claims LIST itself stays own-claims
        only, same as `search()`'s attrs projection.

        Audience variants (Task 10, spec §4.2, canonical example): a claim
        the caller's audience selector rejects never reaches ``rows`` at all
        (the same ``vis`` fragment used everywhere else). Among the
        SURVIVING rows, :func:`_pick_most_privileged` then collapses each
        ``(fact_id, edge_id, corpus_file_id)`` group down to its highest-
        privilege variant — the "$20k vs <redacted>" shape — WITHOUT ever
        touching a group that is entirely untagged (two genuinely distinct
        untagged quotes about the same file are untouched, never collapsed
        to one). Skipped entirely for an admin caller OR when ``is_revealed``:
        both admin god-mode and a `revealed` correction outrank the audience
        selector (spec §4.3's conflict table — "sees everything, both
        layers") and show every variant, exactly as before this task.

        The cap (TCRD-295): this used to be the ONE read with no row limit,
        deliberately — a bare cap without a truncation signal would have been
        the S6 shortfall oracle ``search()``/``neighbors()`` avoid. It now
        carries the same signal ``search()`` does: ``limit_applied`` is true
        only when the CALLER'S OWN readable, deduplicated set exceeds
        ``limit`` (or the bounded fetch itself filled up), never a statement
        about claims grants hid. The cap is applied AFTER dedup, on rows
        ordered newest-first (``document_date DESC NULLS LAST``, then id), so
        the page a caller gets is the most recent evidence. Measured reason:
        an uncapped hub subject returned 18.5k tokens that every later step
        of a chat turn re-read."""
        limit = max(1, min(limit, MAX_CLAIMS_LIMIT))
        readable = _readable_ids(caller)
        is_admin = readable is None
        all_evidence = _visibility_mode() == "all_evidence"
        tiered_hidden, audience_pairs = _audience_context(caller, readable)

        # `.begin()` (not `.connect()`) so `SET LOCAL` applies to the
        # queries that follow in the same transaction.
        with self._engine.begin() as conn:
            conn.execute(sa.text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))
            kind = self._subject_kind(conn, subject_id)
            if kind is None:
                raise FactNotFound(subject_id)

            status = self._subject_status(
                conn,
                subject_kind=kind,
                subject_id=subject_id,
                is_admin=is_admin,
                all_evidence=all_evidence,
                readable=readable,
                tiered_hidden=tiered_hidden,
                audience_pairs=audience_pairs,
            )
            if not self._is_visible(status):
                raise FactNotFound(subject_id)

            is_revealed = bool(status["revealed"])
            kind_column = "fact_id" if kind == "fact" else "edge_id"
            vis = self._visibility_predicate("c.corpus_id", is_admin)
            clause = "TRUE" if is_revealed else vis
            sql = sa.text(
                f"""
                SELECT c.id, c.fact_id, c.edge_id, c.corpus_id, c.corpus_file_id, c.audience,
                       c.quote, c.attrs, c.document_date,
                       cf.filename, cf.path, cfs.source_url
                FROM claims c
                JOIN corpus_files cf ON cf.id = c.corpus_file_id
                LEFT JOIN corpus_file_sources cfs ON cfs.corpus_file_id = c.corpus_file_id
                WHERE c.{kind_column} = :subject_id AND ({clause})
                ORDER BY c.document_date DESC NULLS LAST, c.id
                LIMIT :fetch_cap
                """
            )
            params: Dict[str, Any] = {"subject_id": subject_id, "fetch_cap": _CLAIMS_FETCH_CAP}
            if not is_admin and not is_revealed:
                params["readable"] = list(readable)
                params["tiered_hidden"] = tiered_hidden
                params["audience_pairs"] = audience_pairs
            rows = conn.execute(sql, params).mappings().all()

        fetch_capped = len(rows) >= _CLAIMS_FETCH_CAP
        # Dedup is a NON-ADMIN, non-revealed narrowing only: admin god-mode
        # and a `revealed` correction both outrank the audience selector by
        # design (spec §4.3's conflict table — "sees everything, both
        # layers") and must keep every variant, never collapse them.
        if not is_admin and not is_revealed:
            rows = _pick_most_privileged(rows, _class_rank_map())

        limit_applied = fetch_capped or len(rows) > limit
        rows = rows[:limit]
        out = [self._shape_claim(r, is_admin=is_admin, readable=readable, is_revealed=is_revealed) for r in rows]
        return {"claims": out, "revealed": is_revealed, "limit_applied": limit_applied}

    @staticmethod
    def _shape_claim(
        r: Any,
        *,
        is_admin: bool,
        readable: Optional[frozenset],
        is_revealed: bool,
        quote_max: Optional[int] = None,
    ) -> Dict[str, Any]:
        """The wire shape of ONE claim, shared by :meth:`claims` and the
        inline path (:meth:`_inline_claims`) so the two can never drift. A
        `revealed` correction reveals the FACT, not the geography of its
        evidence (spec §4, review tightening 2026-08-28): for a claim whose
        collection the caller cannot read, the document's human identity
        (name/path/URL) is withheld — opaque ids only — and every quote is
        suppressed. ``quote_max`` (inline path only) caps the quote's length
        and marks the cut with ``quote_truncated``."""
        claim_readable = is_admin or r["corpus_id"] in readable
        document: Optional[Dict[str, Any]]
        if claim_readable or not is_revealed:
            document = {"name": r["filename"], "path": r["path"]}
            if r.get("source_url"):
                document["source_url"] = r["source_url"]
        else:
            document = None
        quote = "" if is_revealed else (r["quote"] or "")
        out: Dict[str, Any] = {
            "id": r["id"],
            "corpus_id": r["corpus_id"],
            "corpus_file_id": r["corpus_file_id"],
            "document": document,
            "quote": quote,
            "attrs": _decode_jsonb(r["attrs"]) or {},
            "document_date": r["document_date"].isoformat() if r["document_date"] else None,
        }
        if quote_max is not None and len(quote) > quote_max:
            out["quote"] = quote[:quote_max]
            out["quote_truncated"] = True
        return out

    def _inline_claims(
        self,
        conn,
        *,
        kind: str,
        ids_with_revealed: List[Tuple[str, bool]],
        k: int,
        is_admin: bool,
        readable: Optional[frozenset],
        tiered_hidden: List[str],
        audience_pairs: List[str],
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], bool]:
        """Bounded inline evidence (TCRD-295): for an EXACT, already-visible
        set of subjects (the caller has decided who is listed — this is never
        a visibility gate), the k NEWEST claims each, in one query. Returns
        ``({subject_id: [claim, ...]}, truncated)``.

        Inherits :meth:`claims`'s rules wholesale, in the same order: the
        SQL keeps only rows the caller can read (or every row for a
        `revealed` subject — the same ``vis``-or-revealed clause), the newest
        ``_INLINE_CLAIMS_WINDOW`` rows per subject come back, audience-
        variant dedup (:func:`_pick_most_privileged`, non-admin and non-
        revealed only) runs on THOSE rows, and k is taken AFTER it — so a
        ``full``/``redacted`` pair on one document is one claim, never two
        rows eating the budget, and a caller never sees a variant outside
        their class. :meth:`_shape_claim` then applies the revealed/opaque-
        document rules and the per-quote character cap.

        Budget: subjects are served in the caller's result order until
        ``MAX_INLINE_CLAIMS_TOTAL`` claims have been attached; the first
        subject that would overshoot stops the attachment (its claims and
        every later subject's are omitted, never partially attached) and
        ``truncated`` is returned true so the caller can say so. A subject
        with zero readable claims costs nothing and gets an explicit ``[]``."""
        if not ids_with_revealed or k <= 0:
            return {}, False
        kind_column = "fact_id" if kind == "fact" else "edge_id"
        vis = self._visibility_predicate("c.corpus_id", is_admin)
        sql = sa.text(
            f"""
            WITH target_ids AS (
                SELECT * FROM unnest(CAST(:ids AS text[]), CAST(:revealed AS bool[])) AS t(subject_id, revealed)
            ),
            ranked AS (
                SELECT t.subject_id, t.revealed,
                       c.id, c.fact_id, c.edge_id, c.corpus_id, c.corpus_file_id, c.audience,
                       c.quote, c.attrs, c.document_date,
                       cf.filename, cf.path, cfs.source_url,
                       ROW_NUMBER() OVER (
                           PARTITION BY t.subject_id ORDER BY c.document_date DESC NULLS LAST, c.id
                       ) AS rn
                FROM target_ids t
                JOIN claims c ON c.{kind_column} = t.subject_id
                JOIN corpus_files cf ON cf.id = c.corpus_file_id
                LEFT JOIN corpus_file_sources cfs ON cfs.corpus_file_id = c.corpus_file_id
                WHERE t.revealed OR ({vis})
            )
            SELECT * FROM ranked WHERE rn <= :window ORDER BY subject_id, rn
            """
        )
        params: Dict[str, Any] = {
            "ids": [i for i, _r in ids_with_revealed],
            "revealed": [bool(r) for _i, r in ids_with_revealed],
            "window": _INLINE_CLAIMS_WINDOW,
        }
        if not is_admin:
            params["readable"] = list(readable) if readable else []
            params["tiered_hidden"] = tiered_hidden or []
            params["audience_pairs"] = audience_pairs or []
        rows = conn.execute(sql, params).mappings().all()

        by_subject: Dict[str, List[Any]] = {}
        for r in rows:
            by_subject.setdefault(r["subject_id"], []).append(r)
        class_rank = _class_rank_map() if not is_admin else {}

        out: Dict[str, List[Dict[str, Any]]] = {}
        total = 0
        truncated = False
        for subject_id, revealed in ids_with_revealed:
            subject_rows = by_subject.get(subject_id, [])
            if subject_rows and not is_admin and not revealed:
                subject_rows = _pick_most_privileged(subject_rows, class_rank)
            subject_rows = subject_rows[:k]
            if not subject_rows:
                out[subject_id] = []
                continue
            if total + len(subject_rows) > MAX_INLINE_CLAIMS_TOTAL:
                truncated = True
                break
            total += len(subject_rows)
            out[subject_id] = [
                self._shape_claim(
                    r, is_admin=is_admin, readable=readable, is_revealed=revealed, quote_max=INLINE_QUOTE_MAX_CHARS
                )
                for r in subject_rows
            ]
        return out, truncated

    # ------------------------------------------------------------------
    # edges — the relationship-shaped read (TCRD-295)
    # ------------------------------------------------------------------

    def edges(
        self,
        caller,
        *,
        edge_type: str,
        src_type: Optional[str] = None,
        dst_type: Optional[str] = None,
        src_id: Optional[str] = None,
        dst_id: Optional[str] = None,
        limit: int = MAX_EDGES_LIMIT,
        extend_edge_type: Optional[str] = None,
        extend_from: str = "dst",
        include_claims: int = 0,
    ) -> Dict[str, Any]:
        """Every visible edge of ONE type, with both endpoints carried as
        full subjects — the relationship-shaped read the five primitives
        lacked (TCRD-295). "Which X relate to which Y" was one
        :meth:`neighbors` call per root plus one :meth:`claims` call per
        citation; here it is one call: ``edge_type`` (required — the names
        come from ``count_visible_edges_by_type`` / `fact_type_map`, never
        from a customer vocabulary baked in here, spec §11), optional
        endpoint type filters (``src_type``/``dst_type``) and anchors
        (``src_id``/``dst_id``), a page ``limit`` (``MAX_EDGES_LIMIT``),
        and an optional ONE-hop ``extend_edge_type`` followed from every
        listed edge's ``extend_from`` endpoint (``"src"`` or ``"dst"``) —
        the second hop of a two-hop question in the same response.

        Visibility (spec §5), all of it in SQL, before ``LIMIT``:
        - the edge itself passes :meth:`_visible_edges_for_corpus_cte`'s
          gate over ALL collections (own readable claim, or `revealed`;
          withheld excluded) — S3, never inferred from its endpoints;
        - BOTH endpoints pass :meth:`_fact_visible_sql` — the same rule
          :meth:`neighbors` applies per hop, so an edge into a withheld or
          unreadable fact is not listed and never reveals it exists (rule 3);
        - ``truncated.result`` / ``truncated.extension`` are computed from
          the caller's OWN visible set (``limit + 1`` fetched), never from
          what grants hid (S6). Absence of a type and a type the caller
          cannot see read identically: an empty page (rule 2).

        Projection runs AFTER truncation (spec §12 cost note) through the
        same :meth:`_project_subjects` ``search()``/``neighbors()`` use, so
        a node here carries byte-identical ``attrs``/``aliases``/
        ``claim_count`` to a search subject. ``include_claims=k`` (≤
        ``MAX_INLINE_CLAIMS_K``) attaches the k newest readable claims to
        each EDGE via :meth:`_inline_claims` (the relationship is what gets
        cited; nodes keep ``claim_count`` and `fact_claims` on demand),
        under the response budget, flagged on ``truncated.claims``.

        Returns ``{"nodes": [<subject>], "edges": [{"id", "src", "dst",
        "type", "attrs", "claims"?}], "truncated": {"result", "extension",
        "claims"}}``. Runs under the shared statement timeout."""
        if not edge_type or not str(edge_type).strip():
            raise ValueError("edge_type is required")
        if extend_from not in ("src", "dst"):
            raise ValueError("extend_from must be 'src' or 'dst'")
        limit = max(1, min(limit, MAX_EDGES_LIMIT))
        include_claims = max(0, min(include_claims, MAX_INLINE_CLAIMS_K))

        readable = _readable_ids(caller)
        is_admin = readable is None
        all_evidence = _visibility_mode() == "all_evidence"
        tiered_hidden, audience_pairs = _audience_context(caller, readable)
        gate = dict(
            is_admin=is_admin,
            all_evidence=all_evidence,
            readable=readable,
            tiered_hidden=tiered_hidden,
            audience_pairs=audience_pairs,
        )
        truncated = {"result": False, "extension": False, "claims": False}

        with self._engine.begin() as conn:
            conn.execute(sa.text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))
            rows, truncated["result"] = self._edge_rows(
                conn,
                edge_type=edge_type,
                src_type=src_type,
                dst_type=dst_type,
                src_ids=[src_id] if src_id else None,
                dst_ids=[dst_id] if dst_id else None,
                limit=limit,
                **gate,
            )
            rows = list(rows)
            if extend_edge_type and rows:
                anchors = sorted({r[extend_from] for r in rows})
                seen = {r["id"] for r in rows}
                ext_rows, truncated["extension"] = self._edge_rows(
                    conn, edge_type=extend_edge_type, anchor_ids=anchors, limit=limit, **gate
                )
                rows.extend(r for r in ext_rows if r["id"] not in seen)

            # Both endpoints of every listed edge are visible (gated in SQL
            # above); collect them in first-seen order for a stable response.
            node_types: Dict[str, str] = {}
            for r in rows:
                node_types.setdefault(r["src"], r["src_type"])
                node_types.setdefault(r["dst"], r["dst_type"])
            node_ids = list(node_types)
            revealed_nodes: set = set()
            if node_ids:
                revealed_nodes = set(
                    conn.execute(
                        sa.text(
                            "SELECT subject_id FROM corrections "
                            "WHERE subject_kind = 'fact' AND verdict = 'revealed' AND subject_id = ANY(:ids)"
                        ),
                        {"ids": node_ids},
                    )
                    .scalars()
                    .all()
                )
            proj_gate = dict(
                is_admin=is_admin, readable=readable, tiered_hidden=tiered_hidden, audience_pairs=audience_pairs
            )
            node_proj = self._project_subjects(
                conn,
                kind="fact",
                ids_with_revealed=[(nid, nid in revealed_nodes) for nid in node_ids],
                with_aliases=True,
                **proj_gate,
            )
            edges_with_revealed = [(r["id"], bool(r["revealed"])) for r in rows]
            edge_proj = self._project_subjects(
                conn, kind="edge", ids_with_revealed=edges_with_revealed, with_aliases=False, **proj_gate
            )
            inline: Dict[str, List[Dict[str, Any]]] = {}
            if include_claims > 0:
                inline, truncated["claims"] = self._inline_claims(
                    conn, kind="edge", ids_with_revealed=edges_with_revealed, k=include_claims, **proj_gate
                )

        nodes_out = []
        for nid in node_ids:
            proj = node_proj.get(nid, {"aliases": [], "attrs": {}, "claim_count": 0})
            revealed = nid in revealed_nodes
            nodes_out.append(
                {
                    "id": nid,
                    "type": node_types[nid],
                    "aliases": proj["aliases"],
                    "attrs": proj["attrs"],
                    "claim_count": proj["claim_count"],
                    "quote_count": 0 if revealed else proj["claim_count"],
                    "revealed": revealed,
                }
            )
        edges_out = []
        for r in rows:
            e: Dict[str, Any] = {
                "id": r["id"],
                "src": r["src"],
                "dst": r["dst"],
                "type": r["type"],
                "attrs": edge_proj.get(r["id"], {"attrs": {}})["attrs"],
            }
            if include_claims > 0 and r["id"] in inline:
                e["claims"] = inline[r["id"]]
            edges_out.append(e)
        return {"nodes": nodes_out, "edges": edges_out, "truncated": truncated}

    def _edge_rows(
        self,
        conn,
        *,
        edge_type: str,
        limit: int,
        is_admin: bool,
        all_evidence: bool,
        readable: Optional[frozenset],
        tiered_hidden: List[str],
        audience_pairs: List[str],
        src_type: Optional[str] = None,
        dst_type: Optional[str] = None,
        src_ids: Optional[List[str]] = None,
        dst_ids: Optional[List[str]] = None,
        anchor_ids: Optional[List[str]] = None,
    ) -> Tuple[List[Any], bool]:
        """One page of visible edges of ``edge_type`` (see :meth:`edges` for
        the gate). ``anchor_ids`` matches EITHER endpoint — the extension
        hop follows a relationship in whichever direction it is stored.
        Returns ``(rows, truncated)`` with ``limit + 1`` fetched so the
        truncation flag is the caller's own shortfall (S6)."""
        cte = self._visible_edges_for_corpus_cte(is_admin, all_collections=True)
        src_ok = self._fact_visible_sql("e.src", is_admin=is_admin, all_evidence=all_evidence)
        dst_ok = self._fact_visible_sql("e.dst", is_admin=is_admin, all_evidence=all_evidence)
        clauses = ["e.type = :edge_type"]
        params: Dict[str, Any] = {"edge_type": edge_type, "limit_plus_one": limit + 1}
        if src_type:
            clauses.append("fs.type = :src_type")
            params["src_type"] = src_type
        if dst_type:
            clauses.append("fd.type = :dst_type")
            params["dst_type"] = dst_type
        if src_ids:
            clauses.append("e.src = ANY(:src_ids)")
            params["src_ids"] = list(src_ids)
        if dst_ids:
            clauses.append("e.dst = ANY(:dst_ids)")
            params["dst_ids"] = list(dst_ids)
        if anchor_ids:
            clauses.append("(e.src = ANY(:anchor_ids) OR e.dst = ANY(:anchor_ids))")
            params["anchor_ids"] = list(anchor_ids)
        if not is_admin:
            params["readable"] = list(readable) if readable else []
            params["tiered_hidden"] = tiered_hidden or []
            params["audience_pairs"] = audience_pairs or []
        sql = sa.text(
            f"""
            WITH {cte}
            SELECT e.id, e.src, e.dst, e.type, fs.type AS src_type, fd.type AS dst_type,
                   (e.id IN (SELECT subject_id FROM edge_revealed_ids)) AS revealed
            FROM edge_visible v
            JOIN edges e ON e.id = v.subject_id
            JOIN facts fs ON fs.id = e.src
            JOIN facts fd ON fd.id = e.dst
            WHERE {" AND ".join(clauses)}
              AND {src_ok}
              AND {dst_ok}
            ORDER BY e.id
            LIMIT :limit_plus_one
            """
        )
        rows = conn.execute(sql, params).mappings().all()
        return rows[:limit], len(rows) > limit

    def _subject_kind(self, conn, subject_id: str) -> Optional[str]:
        row = conn.execute(sa.text("SELECT 1 FROM facts WHERE id = :id"), {"id": subject_id}).first()
        if row is not None:
            return "fact"
        row = conn.execute(sa.text("SELECT 1 FROM edges WHERE id = :id"), {"id": subject_id}).first()
        if row is not None:
            return "edge"
        return None

    # ------------------------------------------------------------------
    # collection-scoped summaries — spec §13.2 "Surfaces": the Library
    # collection card's "N files · M facts" and the collection-detail facts
    # section. Both are ADDITIONS to this frozen-app-state pair's read
    # surface (the A3 ratchet only forbids a brand-new DuckDB module — an
    # extra method on an existing PG-only repo is unaffected), and both
    # funnel through the SAME `_visible_facts_for_corpus` CTE, so a caller
    # can never see a bigger "M" on the card than the facts section it opens
    # into actually lists.
    # ------------------------------------------------------------------

    def _visible_facts_for_corpus_cte(
        self, is_admin: bool, all_evidence: bool, *, all_collections: bool = False
    ) -> str:
        """SQL for a ``visible(subject_id, is_revealed)`` CTE: facts with at
        least one OWN claim evidenced by ``:corpus_id`` (bound by the
        caller — candidacy is deliberately still OWN-claims-only: "evidenced
        by this collection" names a claim IN it, not a fact reachable only
        via an edge whose claim happens to live elsewhere), withheld ones
        (`wrong`/`restricted`) excluded, `revealed` ones included
        unconditionally, and everything else gated by the SAME
        any_evidence/all_evidence rule `search()` applies — over the
        subject's claims EVERYWHERE (own OR a non-withheld incident edge's,
        module docstring "Endpoint evidence"), not just this corpus, so a
        candidate whose only OWN claim here is itself unreadable can still
        be revealed by a readable incident-edge claim. Returned as a
        fragment (no leading ``WITH``) so callers can embed it beside their
        own CTEs; every caller must bind ``:corpus_id`` and, when
        ``is_admin`` is False, ``:readable``, ``:tiered_hidden`` and
        ``:audience_pairs`` (:func:`_audience_context` — Task 10's audience
        selector rides the same ``vis``/``vis3``/``vis_ec`` fragments below).

        With ``all_collections=True`` the ONLY change is candidacy: every
        non-withheld fact instead of the ones evidenced by a bound
        ``:corpus_id`` (which must then NOT be bound), giving the same
        population ``search()`` gates with ``type=None``. The visibility
        rule below is shared verbatim rather than restated for the wider
        scope — a second copy is how the two drift, and drift in this gate
        is the S2 existence oracle the spec's rev 1->2 closed.

        **Candidate SOURCE (TCRD-296 E.21).** The "at least one OWN claim"
        existence check below reads `fact_collection_membership` — a row
        exists there iff a matching claim exists in `claims`, maintained by
        `_bump_collection_stats_on_new_claim`/`rebuild_collection_stats` —
        rather than scanning `claims` itself (5-8s per request over a
        2M-row table under load, TCRD-296 synthesis E.21). This changes
        WHERE candidacy is read from, never WHAT counts as a candidate: the
        same "any claim in scope" universe, just a smaller, indexed table to
        scan it in. The visibility GATE below (`vis`/`vis3`/`vis_ec`, via
        `_visibility_predicate`/`_audience_context`) is completely
        unchanged. Falls back to the original `claims` scan, unconditionally,
        whenever `fact_collection_stats` is still empty (nothing has been
        rebuilt since the migration that created these tables) — see
        `rebuild_collection_stats`'s docstring.

        **Admin fast path (TCRD-296 gap #70).** When `is_admin` is True and
        `all_evidence` is False, `visible` collapses to `candidates` verbatim
        (plus the `is_revealed` flag) instead of re-verifying every candidate
        against `vis`/`vis_ec` via `EXISTS` — the per-candidate probe over
        532k rows that cost 9.5s on a live graph (`count_visible_facts_by_
        type`, TCRD-296 gap #70). This is a SPECIALISATION of the same gate,
        never a relaxation of it: `_visibility_predicate` returns
        unconditional `TRUE` for an admin, so the `NOT all_evidence` disjunct
        collapses to `EXISTS (SELECT 1 FROM claims c2 WHERE c2.fact_id =
        cand.subject_id)` — and every row already in `candidates` satisfies
        that BY CONSTRUCTION, whichever half of `candidate_ids` produced it:
        a membership-sourced candidate exists in `fact_collection_membership`
        only because `_bump_collection_stats_impl`/`_rebuild_one_collection_
        stats` insert/rebuild that table FROM a `GROUP BY` over `claims` on
        the same `fact_id` — the membership row is itself proof a matching
        `claims` row exists (see those methods' bodies). A fallback-sourced
        candidate (stats table still empty) is read directly off `claims c`
        (`SELECT DISTINCT c.fact_id`), so it IS such a row. Either way the
        `EXISTS` is trivially true, `endpoint_claims` never needs building
        (skipped entirely in this branch — one more join this path no longer
        pays for), and `visible` reduces to `candidates` MINUS NOTHING
        FURTHER: `wrong`/`restricted` subjects are already excluded upstream
        in `candidates`, and a `revealed` correction adds no subject an admin
        could not already see, so ORing `revealed_ids` back in is a no-op.
        Deliberately scoped to `all_evidence=False` only — the
        `all_evidence=True` branch keeps its own `NOT EXISTS` checks
        unchanged below, out of caution, even though the same admin
        short-circuit makes them vacuous too. The non-fast-path SQL text
        below (non-admin, or `all_evidence=True`) is unchanged."""
        vis = self._visibility_predicate("c2.corpus_id", is_admin)
        vis3 = self._visibility_predicate("c3.corpus_id", is_admin)
        vis_ec = self._visibility_predicate("ec.corpus_id", is_admin)
        all_ev_sql = "TRUE" if all_evidence else "FALSE"
        candidate_where = (
            "c.fact_id IS NOT NULL" if all_collections else "c.corpus_id = :corpus_id AND c.fact_id IS NOT NULL"
        )
        membership_where = "TRUE" if all_collections else "corpus_id = :corpus_id"
        candidates_sql = f"""
            candidate_ids AS (
                SELECT fact_id AS subject_id FROM fact_collection_membership
                WHERE {membership_where} AND EXISTS (SELECT 1 FROM fact_collection_stats)
                UNION
                SELECT DISTINCT c.fact_id AS subject_id
                FROM claims c
                WHERE {candidate_where} AND NOT EXISTS (SELECT 1 FROM fact_collection_stats)
            ),
            candidates AS (
                SELECT ci.subject_id
                FROM candidate_ids ci
                WHERE NOT EXISTS (
                    SELECT 1 FROM corrections co
                    WHERE co.subject_kind = 'fact' AND co.subject_id = ci.subject_id
                      AND co.verdict IN ('wrong', 'restricted')
                  )
            ),
            revealed_ids AS (
                SELECT subject_id FROM corrections WHERE subject_kind = 'fact' AND verdict = 'revealed'
            )"""
        if is_admin and not all_evidence:
            return (
                candidates_sql
                + """,
            visible AS (
                SELECT cand.subject_id,
                       (cand.subject_id IN (SELECT subject_id FROM revealed_ids)) AS is_revealed
                FROM candidates cand
            )
            """
            )
        return (
            candidates_sql
            + f""",
            endpoint_claims AS (
                -- `audience` selected alongside `corpus_id` so `vis_ec`
                -- (Task 10's audience selector) has a column to read off
                -- this alias — same reasoning as `search()`'s own copy.
                SELECT DISTINCT cand.subject_id AS subject_id, c.corpus_id AS corpus_id, c.audience AS audience
                FROM candidates cand
                JOIN edges e ON (e.src = cand.subject_id OR e.dst = cand.subject_id)
                JOIN claims c ON c.edge_id = e.id
                WHERE NOT EXISTS (
                    SELECT 1 FROM corrections co
                    WHERE co.subject_kind = 'edge' AND co.subject_id = e.id
                      AND co.verdict IN ('wrong', 'restricted')
                )
            ),
            visible AS (
                SELECT cand.subject_id,
                       (cand.subject_id IN (SELECT subject_id FROM revealed_ids)) AS is_revealed
                FROM candidates cand
                WHERE cand.subject_id IN (SELECT subject_id FROM revealed_ids)
                   OR (
                        NOT {all_ev_sql}
                        AND (
                            EXISTS (SELECT 1 FROM claims c2 WHERE c2.fact_id = cand.subject_id AND {vis})
                            OR EXISTS (SELECT 1 FROM endpoint_claims ec WHERE ec.subject_id = cand.subject_id AND {vis_ec})
                        )
                   )
                   OR (
                        {all_ev_sql}
                        AND (
                            EXISTS (SELECT 1 FROM claims c2 WHERE c2.fact_id = cand.subject_id)
                            OR EXISTS (SELECT 1 FROM endpoint_claims ec WHERE ec.subject_id = cand.subject_id)
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM claims c3
                            WHERE c3.fact_id = cand.subject_id AND NOT ({vis3})
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM endpoint_claims ec WHERE ec.subject_id = cand.subject_id AND NOT ({vis_ec})
                        )
                   )
            )
            """
        )

    def count_visible_facts_by_type(self, caller) -> Dict[str, int]:
        """Caller-scoped ``{type: count}`` over every visible fact — the
        Library type map's live counts (spec §13.2 "Library"), where each
        type is a way in to the graph.

        Shares :meth:`_visible_facts_for_corpus_cte`'s gate via
        ``all_collections=True``, so a type's number counts exactly the
        subjects this caller could reach through ``search(type=...)`` and
        never more. A naive ``SELECT type, COUNT(*) FROM facts GROUP BY
        type`` would be the S2 existence oracle in aggregate form: it would
        report facts whose every claim sits in a collection the caller
        cannot read, letting a reader count what they cannot see. A type
        with no visible subjects is omitted rather than reported as 0 — the
        caller cannot distinguish "no such type in this ontology" from
        "none you can see", which is the same non-disclosure
        ``search()`` makes.
        """
        readable = _readable_ids(caller)
        is_admin = readable is None
        all_evidence = _visibility_mode() == "all_evidence"
        tiered_hidden, audience_pairs = _audience_context(caller, readable)
        cte = self._visible_facts_for_corpus_cte(is_admin, all_evidence, all_collections=True)
        params: Dict[str, Any] = (
            {}
            if is_admin
            else {"readable": list(readable), "tiered_hidden": tiered_hidden, "audience_pairs": audience_pairs}
        )
        sql = sa.text(
            f"WITH {cte} "
            "SELECT f.type, COUNT(*) AS n FROM visible v JOIN facts f ON f.id = v.subject_id "
            "GROUP BY f.type ORDER BY f.type"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
        return {r["type"]: int(r["n"]) for r in rows}

    def count_visible_edges_by_type(self, caller) -> Dict[str, int]:
        """Caller-scoped ``{type: count}`` over every visible EDGE — the
        edge-type discovery row `fact_type_map` grew alongside its existing
        node-type counts (spec §12), so an agent can learn a valid
        `fact_neighbors(edge_types=[...])` value without first pulling every
        relationship off a well-connected node (a token-burn shape a live
        run surfaced: "which industries is X in" cost an unfiltered, every-
        edge-type traversal because there was no cheap way to learn the
        `in_industry` edge type name up front).

        Same non-disclosure the fact side makes: a type with no visible
        edges is omitted rather than reported as 0, and the gate is shared
        verbatim via :meth:`_visible_edges_for_corpus_cte`'s
        ``all_collections=True`` — never a naive
        ``SELECT type, COUNT(*) FROM edges GROUP BY type``, which would be
        the S2 existence oracle for edges."""
        readable = _readable_ids(caller)
        is_admin = readable is None
        tiered_hidden, audience_pairs = _audience_context(caller, readable)
        cte = self._visible_edges_for_corpus_cte(is_admin, all_collections=True)
        params: Dict[str, Any] = (
            {}
            if is_admin
            else {"readable": list(readable), "tiered_hidden": tiered_hidden, "audience_pairs": audience_pairs}
        )
        # An edge's own evidence is not the whole of what makes it reachable:
        # `neighbors` resolves the OTHER endpoint per hop and skips the edge
        # when that fact is withheld, so an edge into a `wrong`/`restricted`
        # fact was counted here while no traversal could ever produce it — and
        # for a type that is empty once those are removed, a nonzero count is
        # exactly the existence oracle the docstring above says this must not
        # be (Devin Review on #2079).
        #
        # Deliberately narrowed to the endpoint CORRECTIONS rather than
        # composed with `_visible_facts_for_corpus_cte`: that CTE requires a
        # fact to carry evidence in its own right, which is STRICTER than the
        # endpoint test `neighbors` applies — a fact evidenced only through
        # the incident edge passes there and would vanish here, making the map
        # under-count edges the caller really can traverse. Matching the
        # over-count with an under-count is not a fix.
        endpoint_gate = (
            " WHERE NOT EXISTS ("
            "SELECT 1 FROM corrections co WHERE co.subject_kind = 'fact' "
            "AND co.subject_id IN (e.src, e.dst) "
            "AND co.verdict IN ('wrong', 'restricted') "
            "AND NOT EXISTS ("
            "SELECT 1 FROM corrections cr WHERE cr.subject_kind = 'fact' "
            "AND cr.subject_id = co.subject_id AND cr.verdict = 'revealed'))"
        )
        sql = sa.text(
            f"WITH {cte} "
            "SELECT e.type, COUNT(*) AS n FROM edge_visible v JOIN edges e ON e.id = v.subject_id"
            f"{endpoint_gate} "
            "GROUP BY e.type ORDER BY e.type"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, params).mappings().all()
        return {r["type"]: int(r["n"]) for r in rows}

    def facet_values(self, caller, *, types: List[str], limit_per_type: int = 50) -> Dict[str, List[Dict[str, Any]]]:
        """Filterable entity values per type, with how many DOCUMENTS each
        one is evidenced by — the Library's entity facets (spec §13.2
        "Library", TCRD-250 piece 4).

        This is what "filter by tags" should have meant here: the vocabulary
        the extraction pass already produces (client, industry, service
        offering, document type), rather than tags nobody maintains.

        Same visibility gate as :meth:`count_visible_facts_by_type` — shared
        via ``all_collections=True``, never restated — so a facet lists only
        subjects this caller could reach through ``search(type=...)``.

        Two deliberate conservatisms, both about not leaking a count:

        * The document tally counts only claims whose collection the caller
          can READ, even for a subject carrying a ``revealed`` correction.
          A revealed correction reveals the SUBJECT, not the geography of
          its evidence (spec §4) — counting its unreadable documents would
          report how many files exist in a collection the caller cannot
          open.
        * A subject whose readable document count is 0 is still listed when
          it is visible, because visibility may come from an incident edge
          (endpoint evidence). Its count is an honest 0, not an omission —
          the caller CAN reach the subject, just not any document naming it.

        ``limit_per_type`` caps each facet's value list; a facet menu is a
        menu, not a dump.
        """
        if not types:
            return {}
        readable = _readable_ids(caller)
        is_admin = readable is None
        all_evidence = _visibility_mode() == "all_evidence"
        tiered_hidden, audience_pairs = _audience_context(caller, readable)
        cte = self._visible_facts_for_corpus_cte(is_admin, all_evidence, all_collections=True)
        vis_claims = self._visibility_predicate("c.corpus_id", is_admin)
        alias_readable = self._alias_readable_sql(
            revealed_expr="fa.fact_id IN (SELECT subject_id FROM revealed_ids)", is_admin=is_admin
        )
        params: Dict[str, Any] = {"types": list(types)}
        if not is_admin:
            params["readable"] = list(readable)
            params["tiered_hidden"] = tiered_hidden
            params["audience_pairs"] = audience_pairs
        sql = sa.text(
            f"""
            WITH {cte},
            readable_claims AS (
                SELECT c.fact_id, c.corpus_file_id
                FROM claims c
                JOIN visible v ON v.subject_id = c.fact_id
                WHERE {vis_claims}
            ),
            labels AS (
                SELECT fa.fact_id, MIN(fa.natural_key) AS label
                FROM fact_aliases fa
                WHERE fa.fact_id IN (SELECT subject_id FROM visible)
                  AND {alias_readable}
                GROUP BY fa.fact_id
            )
            SELECT f.type AS type, v.subject_id AS subject_id,
                   l.label AS label,
                   COUNT(DISTINCT rc.corpus_file_id) AS n
            FROM visible v
            JOIN facts f ON f.id = v.subject_id
            LEFT JOIN readable_claims rc ON rc.fact_id = v.subject_id
            LEFT JOIN labels l ON l.fact_id = v.subject_id
            WHERE f.type = ANY(:types)
            GROUP BY f.type, v.subject_id, l.label
            ORDER BY f.type, COUNT(DISTINCT rc.corpus_file_id) DESC, v.subject_id
            """
        )
        out: Dict[str, List[Dict[str, Any]]] = {t: [] for t in types}
        with self._engine.connect() as conn:
            for r in conn.execute(sql, params).mappings():
                bucket = out.setdefault(r["type"], [])
                if len(bucket) >= limit_per_type:
                    continue
                bucket.append(
                    {
                        "subject_id": r["subject_id"],
                        "label": r["label"] or r["subject_id"],
                        "document_count": int(r["n"]),
                    }
                )
        return out

    def facet_values_for_collections(
        self, caller, corpus_ids: List[str], *, types: List[str]
    ) -> Dict[str, Dict[str, List[str]]]:
        """``{corpus_id: {type: [label, ...]}}`` — what each collection is
        ABOUT, for the Library's entity facets (spec §13.2 "Library",
        TCRD-250 piece 4).

        :meth:`facet_values` answers "what values exist, and how many
        documents each covers" — the menu's vocabulary. This answers the
        other half the toolbar needs: which values does THIS row carry, so
        the engine can slice rows by them. Both share
        :meth:`_visible_facts_for_corpus_cte`'s gate via
        ``all_collections=True`` rather than restating it, for the reason
        that method's docstring gives: a second copy of the visibility rule
        is how the two drift, and drift in this gate is an existence oracle.

        Narrower than :meth:`facet_values` on one axis, deliberately: a
        subject is attributed to a collection only through a claim whose
        collection the caller can READ. Endpoint-edge visibility is enough
        to reach a subject (so it earns a place in the menu) but not to say
        a particular collection is about it — a row's facet values are a
        claim about that row, and evidence the caller cannot open cannot
        support one. The practical effect is the honest one: a value the
        caller can see in the menu may match no row here.

        Returns only collections that carry at least one value, so a caller
        can `.get(corpus_id, {})` and a graph-less instance costs nothing.
        """
        if not corpus_ids or not types:
            return {}
        readable = _readable_ids(caller)
        is_admin = readable is None
        all_evidence = _visibility_mode() == "all_evidence"
        tiered_hidden, audience_pairs = _audience_context(caller, readable)
        cte = self._visible_facts_for_corpus_cte(is_admin, all_evidence, all_collections=True)
        vis_claims = self._visibility_predicate("c.corpus_id", is_admin)
        alias_readable = self._alias_readable_sql(
            revealed_expr="fa.fact_id IN (SELECT subject_id FROM revealed_ids)", is_admin=is_admin
        )
        params: Dict[str, Any] = {"types": list(types), "corpus_ids": list(corpus_ids)}
        # `readable is not None` rather than `not is_admin`: the two are the
        # same condition by construction (``is_admin = readable is None``
        # above), and spelling it this way lets a type checker see that the
        # bind below is over a real set.
        if readable is not None:
            params["readable"] = list(readable)
            params["tiered_hidden"] = tiered_hidden
            params["audience_pairs"] = audience_pairs
        sql = sa.text(
            f"""
            WITH {cte},
            readable_claims AS (
                SELECT DISTINCT c.fact_id, c.corpus_id
                FROM claims c
                JOIN visible v ON v.subject_id = c.fact_id
                WHERE c.corpus_id = ANY(:corpus_ids) AND {vis_claims}
            ),
            labels AS (
                SELECT fa.fact_id, MIN(fa.natural_key) AS label
                FROM fact_aliases fa
                WHERE fa.fact_id IN (SELECT subject_id FROM visible)
                  AND {alias_readable}
                GROUP BY fa.fact_id
            )
            SELECT rc.corpus_id AS corpus_id, f.type AS type,
                   COALESCE(l.label, rc.fact_id) AS label
            FROM readable_claims rc
            JOIN facts f ON f.id = rc.fact_id
            LEFT JOIN labels l ON l.fact_id = rc.fact_id
            WHERE f.type = ANY(:types)
            GROUP BY rc.corpus_id, f.type, COALESCE(l.label, rc.fact_id)
            ORDER BY rc.corpus_id, f.type, COALESCE(l.label, rc.fact_id)
            """
        )
        out: Dict[str, Dict[str, List[str]]] = {}
        with self._engine.connect() as conn:
            for r in conn.execute(sql, params).mappings():
                out.setdefault(r["corpus_id"], {}).setdefault(r["type"], []).append(r["label"])
        return out

    @staticmethod
    def _facet_top_values_sql(*, q_present: bool, label_where: str) -> str:
        """The tail of :meth:`facet_top_values_for_collections`'s query —
        everything after the shared ``counted`` CTE — as a pure string
        builder (no DB access), so its shape is unit-testable without a
        live Postgres.

        Two shapes, chosen by whether ``q`` (the typeahead route) is
        present:

        * ``q_present=False`` (the Library's default facet-menu request,
          TCRD-296 gap #70): rank FIRST — `ROW_NUMBER() ... <= :limit_
          per_type` straight over ``counted`` — then join `fact_aliases`
          only for the surviving, already-bounded winners (at most
          ``limit_per_type * len(types)`` rows). EXPLAIN ANALYZE on a live
          graph showed the label-then-rank order below scanning EVERY
          counted fact of the requested types (408k rows) into an
          `aliased` HashAggregate and a 48 MB `Sort`, only to discard all
          but the top `limit_per_type` per type — the alias join is now
          bounded by the SAME cut the caller asked for, not by the size of
          the graph.
        * ``q_present=True``: unchanged from before this optimisation — the
          label is needed BEFORE ranking, since ``q`` filters on it, so
          `fact_aliases` is still joined for every counted fact first, then
          filtered by ``label_where``, then ranked.

        Both shapes return the same columns (``type, fact_id, label, n``)
        in the same order (``type, n DESC, fact_id``) — the caller cannot
        tell which branch ran from the result shape."""
        if q_present:
            return f"""
            aliased AS (
                SELECT fact_id, MIN(natural_key) AS label
                FROM fact_aliases
                WHERE fact_id IN (SELECT fact_id FROM counted)
                GROUP BY fact_id
            ),
            labeled AS (
                SELECT counted.type, counted.fact_id,
                       COALESCE(aliased.label, counted.fact_id) AS label, counted.n
                FROM counted
                LEFT JOIN aliased ON aliased.fact_id = counted.fact_id
            ),
            filtered AS (
                SELECT * FROM labeled {label_where}
            ),
            ranked AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY type ORDER BY n DESC, fact_id) AS rn
                FROM filtered
            )
            SELECT type, fact_id, label, n
            FROM ranked
            WHERE rn <= :limit_per_type
            ORDER BY type, n DESC, fact_id
            """
        return """
            ranked AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY type ORDER BY n DESC, fact_id) AS rn
                FROM counted
            ),
            top AS (
                SELECT type, fact_id, n FROM ranked WHERE rn <= :limit_per_type
            ),
            aliased AS (
                SELECT fact_id, MIN(natural_key) AS label
                FROM fact_aliases
                WHERE fact_id IN (SELECT fact_id FROM top)
                GROUP BY fact_id
            )
            SELECT top.type AS type, top.fact_id AS fact_id,
                   COALESCE(aliased.label, top.fact_id) AS label, top.n AS n
            FROM top
            LEFT JOIN aliased ON aliased.fact_id = top.fact_id
            ORDER BY type, n DESC, fact_id
            """

    def facet_top_values_for_collections(
        self,
        corpus_ids: Optional[List[str]],
        *,
        types: List[str],
        limit_per_type: int = 25,
        q: Optional[str] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """``{type: [{fact_id, label, document_count}, ...]}``, top
        ``limit_per_type`` per type by document count — the Library's entity
        FACET MENU vocabulary (spec §13.2), bounded IN SQL rather than
        computed-then-sliced in Python.

        Round 3 of the 2026-09-03 incident: :meth:`facet_values` (the
        API's own facet-vocabulary endpoint) runs a full, unbounded query
        over the caller's ENTIRE visible graph — the exact-visibility CTE,
        no LIMIT — and applies ``limit_per_type`` only after every row has
        already been fetched into Python. On a live instance (397
        collections, 345k facts) the Library index built its facet menu by
        tallying this same unbounded shape off every RENDERED row's own
        per-collection facet list (``facet_values_for_collections``, which
        has the identical "no cap" property), and the two together cost
        18 MB of HTML AND — the second live datum — a slow (4.6s TTFB)
        response even for a caller whose OWN visible set was small: the
        candidate scan itself, not the output size, was the expensive part.

        Two deliberate departures from the exact per-caller CTE, both
        already precedented by :meth:`approximate_counts_for_collections`
        (same incident, admin fact-count half):

        * Scoped by ``corpus_ids`` alone (``None`` = no filter, the
          admin/"see everything" case) — an indexed ``claims.corpus_id``
          scan, never the ``NOT EXISTS`` correction check or the
          audience-tiering join `_visible_facts_for_corpus_cte` pays for
          every candidate row regardless of which ones survive. The RBAC
          boundary is `corpus_ids` itself: every caller of this method
          passes a set already vetted by ownership/grant checks (or `None`
          only when the caller IS unrestricted), so narrowing further here
          would be redundant, not safer.
        * A `wrong`/`restricted` correction is not excluded (the "approximate"
          trade the sibling method already makes) — a small, typically-empty
          set relative to a facet menu, and this is a MENU of filter
          OPTIONS, not a disclosure of specific evidence.

        ``q`` (used by the typeahead route, `GET /library/facets/{facet}`)
        narrows to labels matching it, ranked the same way (by document
        count) rather than alphabetically — the most useful few matches
        first. `statement_timeout` is set for this call alone.

        **TCRD-296 E.21**: ``n`` (document count per fact) is read from
        `fact_collection_membership.documents_count`, summed across every
        matching ``corpus_id`` — algebraically identical to the old
        `COUNT(DISTINCT corpus_file_id)` over `claims`, since a
        `corpus_files` row belongs to exactly one collection (disjoint
        document sets per collection, so a per-collection distinct-document
        count sums cleanly across collections). Falls back to the original
        `claims` scan, unconditionally, whenever `fact_collection_stats` is
        still empty — see `rebuild_collection_stats`'s docstring.

        **Rank first, label last (TCRD-296 gap #70).** Without ``q`` (the
        Library's default facet-menu request), :meth:`_facet_top_values_sql`
        applies the ``ROW_NUMBER() ... <= limit_per_type`` cut BEFORE joining
        `fact_aliases`, rather than after — EXPLAIN ANALYZE on a live graph
        showed the old order labeling and sorting EVERY counted fact of the
        requested types (408k rows, a 48 MB `Sort`) only to discard all but
        the top `limit_per_type` per type. `q` (the typeahead route) needs
        the label to filter ON, so that branch keeps the original label-
        then-rank order — see that method's own docstring.
        """
        if not types:
            return {}
        params: Dict[str, Any] = {"types": list(types), "limit_per_type": limit_per_type}
        corpus_filter = ""
        membership_filter = ""
        if corpus_ids is not None:
            corpus_filter = "c.corpus_id = ANY(:corpus_ids) AND "
            membership_filter = "corpus_id = ANY(:corpus_ids) AND "
            params["corpus_ids"] = list(corpus_ids)
        q_norm = (q or "").strip()
        label_where = ""
        if q_norm:
            label_where = "WHERE label ILIKE :q"
            params["q"] = f"%{q_norm}%"
        sql = sa.text(
            f"""
            WITH counted_from_membership AS (
                SELECT fact_id, SUM(documents_count) AS n
                FROM fact_collection_membership
                WHERE {membership_filter}EXISTS (SELECT 1 FROM fact_collection_stats)
                GROUP BY fact_id
            ),
            scoped_claims AS (
                SELECT DISTINCT c.fact_id, c.corpus_file_id
                FROM claims c
                WHERE {corpus_filter}c.fact_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM fact_collection_stats)
            ),
            counted_from_claims AS (
                SELECT fact_id, COUNT(DISTINCT corpus_file_id) AS n
                FROM scoped_claims
                GROUP BY fact_id
            ),
            counted AS (
                SELECT f.type AS type, m.fact_id AS fact_id, m.n AS n
                FROM (
                    SELECT fact_id, n FROM counted_from_membership
                    UNION ALL
                    SELECT fact_id, n FROM counted_from_claims
                ) m
                JOIN facts f ON f.id = m.fact_id
                WHERE f.type = ANY(:types)
            ),
            {self._facet_top_values_sql(q_present=bool(q_norm), label_where=label_where)}
            """
        )
        out: Dict[str, List[Dict[str, Any]]] = {t: [] for t in types}
        with self._engine.begin() as conn:
            conn.execute(sa.text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))
            for r in conn.execute(sql, params).mappings():
                out.setdefault(r["type"], []).append(
                    {"fact_id": r["fact_id"], "label": r["label"], "document_count": int(r["n"])}
                )
        return out

    def facet_membership_for_collections(
        self, corpus_ids: List[str], fact_ids: List[str]
    ) -> Dict[str, Dict[str, List[str]]]:
        """``{corpus_id: {type: [label, ...]}}`` restricted to the given
        ``fact_ids`` — the per-ROW counterpart to
        :meth:`facet_top_values_for_collections`: a row only needs to
        declare membership in a value the (already bounded) facet MENU can
        actually offer, so this is called with exactly that method's own
        output ids rather than the caller's full graph. Both `corpus_ids`
        and `fact_ids` are expected small (a rendered page of collections, a
        `limit_per_type`-bounded value list), so this is a plain indexed
        join with no ranking, no window function and no per-caller CTE.
        """
        if not corpus_ids or not fact_ids:
            return {}
        sql = sa.text(
            """
            SELECT DISTINCT c.corpus_id AS corpus_id, f.type AS type,
                   COALESCE(a.label, f.id) AS label
            FROM claims c
            JOIN facts f ON f.id = c.fact_id
            LEFT JOIN (
                SELECT fact_id, MIN(natural_key) AS label
                FROM fact_aliases
                WHERE fact_id = ANY(:fact_ids)
                GROUP BY fact_id
            ) a ON a.fact_id = f.id
            WHERE c.corpus_id = ANY(:corpus_ids) AND c.fact_id = ANY(:fact_ids)
            """
        )
        out: Dict[str, Dict[str, List[str]]] = {}
        with self._engine.connect() as conn:
            for r in conn.execute(sql, {"corpus_ids": list(corpus_ids), "fact_ids": list(fact_ids)}).mappings():
                out.setdefault(r["corpus_id"], {}).setdefault(r["type"], []).append(r["label"])
        return out

    def count_visible_facts_for_collection(self, caller, corpus_id: str) -> int:
        """Caller-scoped count of facts evidenced by ``corpus_id`` — the
        Library collection card's "M facts" number (spec §13.2 "Library").
        Cheap: one indexed-`corpus_id` query, no traversal. Two callers with
        different grants on the SAME collection can see a different M: under
        `all_evidence` mode a fact whose OTHER claims live in a collection
        one caller cannot reach is hidden from them but visible to a caller
        who can reach every collection it is evidenced from; a caller with no
        access to `corpus_id` at all (not gated here — the caller of this
        method is expected to have already confirmed collection access)
        would still see only `revealed` subjects, if any."""
        return self.count_visible_facts_for_collections(caller, [corpus_id]).get(corpus_id, 0)

    def count_visible_facts_for_collections(self, caller, corpus_ids: List[str]) -> Dict[str, int]:
        """Batch form of :meth:`count_visible_facts_for_collection` — same
        per-collection numbers, but the caller's readable set is resolved
        **once** for the whole batch.

        That resolution is the expensive half. ``_readable_ids`` delegates to
        ``accessible_collection_ids``, which for a non-admin runs a grants
        query plus a full owned-collections scan; calling the singular method
        in a loop (the Library page renders one card per collection) repeated
        that scan per card, so the page cost grew with the number of
        collections the caller can see. The count query itself is one
        indexed-``corpus_id`` lookup and stays per collection, now over a
        single shared connection.

        Visibility still resolves entirely in here — callers pass ids, never
        a precomputed readable set, so no route can widen what it sees
        (spec §5).
        """
        readable = _readable_ids(caller)
        is_admin = readable is None
        all_evidence = _visibility_mode() == "all_evidence"
        tiered_hidden, audience_pairs = _audience_context(caller, readable)
        base: Dict[str, Any] = (
            {}
            if is_admin
            else {"readable": list(readable), "tiered_hidden": tiered_hidden, "audience_pairs": audience_pairs}
        )
        sql = sa.text(f"WITH {self._visible_facts_for_corpus_cte(is_admin, all_evidence)} SELECT COUNT(*) FROM visible")

        out: Dict[str, int] = {}
        if not corpus_ids:
            return out
        with self._engine.connect() as conn:
            for corpus_id in corpus_ids:
                row = conn.execute(sql, {**base, "corpus_id": corpus_id}).first()
                out[corpus_id] = int(row[0]) if row else 0
        return out

    def _visible_edges_for_corpus_cte(self, is_admin: bool, *, all_collections: bool = False) -> str:
        """SQL for an ``edge_visible(subject_id)`` CTE over EDGES evidenced
        by ``:corpus_id`` — the edge analogue of
        :meth:`_visible_facts_for_corpus_cte`, used by
        :meth:`count_visible_edges_for_collections` (the source card's
        pipeline-strip "edges" number, spec §13.2) and, with
        ``all_collections=True``, :meth:`count_visible_edges_by_type` (the
        `fact_type_map` edge-type discovery row — spec §12 query-surface
        note). Unlike a fact, an edge's own claim IS its only evidence path
        — there is no endpoint-claim fallback — so this candidacy/visibility
        rule is simpler: any_evidence semantics only (matching
        :meth:`neighbors`'s own edge-visibility rule — an edge needs its OWN
        readable claim, never inferred from its endpoints), withheld
        (``wrong``/``restricted``) edges excluded, ``revealed`` ones
        included unconditionally. Returned as a fragment (no leading
        ``WITH``); every caller must bind, when ``is_admin`` is False,
        ``:readable``, ``:tiered_hidden`` and ``:audience_pairs``
        (:func:`_audience_context`, same as
        :meth:`_visible_facts_for_corpus_cte`) — and, when
        ``all_collections`` is False, ``:corpus_id``.

        With ``all_collections=True`` the ONLY change is candidacy: every
        non-withheld edge with at least one claim instead of the ones
        evidenced by a bound ``:corpus_id`` — same relationship the fact
        CTE's own ``all_collections`` flag has to its per-collection form,
        shared verbatim rather than restated (a second copy is how the two
        drift, and drift in this gate is the S2 existence oracle).

        **Candidate SOURCE (TCRD-296 E.21)** — same swap and same fallback
        contract as :meth:`_visible_facts_for_corpus_cte`'s own docstring:
        `edge_collection_membership` instead of scanning `claims`, falling
        back unconditionally when `fact_collection_stats` is still empty.
        The visibility gate (`vis`, via `_visibility_predicate`) is
        unchanged.

        **Admin fast path (TCRD-296 gap #70).** When `is_admin` is True,
        `edge_visible` collapses to `edge_candidates` verbatim instead of
        re-verifying every candidate against `vis` via `EXISTS` — the same
        specialisation :meth:`_visible_facts_for_corpus_cte` documents in
        full, applied here without an `all_evidence` fork because edges have
        only the one (any-evidence) rule: `_visibility_predicate` is
        unconditional `TRUE` for an admin, so `EXISTS (SELECT 1 FROM claims
        c2 WHERE c2.edge_id = cand.subject_id AND TRUE)` is trivially true
        for every `edge_candidates` row by the SAME construction argument
        (an `edge_collection_membership` row, or a fallback row read
        straight off `claims`, is itself proof a matching claim exists) —
        see that method's docstring for the full proof. `wrong`/`restricted`
        edges are already excluded upstream in `edge_candidates`, and ORing
        `edge_revealed_ids` back in adds nothing an admin could not already
        see. The non-admin SQL text below is unchanged."""
        vis = self._visibility_predicate("c2.corpus_id", is_admin)
        candidate_where = (
            "c.edge_id IS NOT NULL" if all_collections else "c.corpus_id = :corpus_id AND c.edge_id IS NOT NULL"
        )
        membership_where = "TRUE" if all_collections else "corpus_id = :corpus_id"
        candidates_sql = f"""
            edge_candidate_ids AS (
                SELECT edge_id AS subject_id FROM edge_collection_membership
                WHERE {membership_where} AND EXISTS (SELECT 1 FROM fact_collection_stats)
                UNION
                SELECT DISTINCT c.edge_id AS subject_id
                FROM claims c
                WHERE {candidate_where} AND NOT EXISTS (SELECT 1 FROM fact_collection_stats)
            ),
            edge_candidates AS (
                SELECT eci.subject_id
                FROM edge_candidate_ids eci
                WHERE NOT EXISTS (
                    SELECT 1 FROM corrections co
                    WHERE co.subject_kind = 'edge' AND co.subject_id = eci.subject_id
                      AND co.verdict IN ('wrong', 'restricted')
                  )
            ),
            edge_revealed_ids AS (
                SELECT subject_id FROM corrections WHERE subject_kind = 'edge' AND verdict = 'revealed'
            )"""
        # `edge_revealed_ids` stays defined in BOTH branches above — some
        # callers (e.g. `_edge_rows`) reference it directly in their own
        # SELECT (a "revealed" display flag) alongside this fragment's
        # `edge_visible`, admin or not, so only the EXISTS(vis) re-check
        # inside `edge_visible` itself is fast-pathed below.
        if is_admin:
            return (
                candidates_sql
                + """,
            edge_visible AS (
                SELECT cand.subject_id
                FROM edge_candidates cand
            )
            """
            )
        return (
            candidates_sql
            + f""",
            edge_visible AS (
                SELECT cand.subject_id
                FROM edge_candidates cand
                WHERE cand.subject_id IN (SELECT subject_id FROM edge_revealed_ids)
                   OR EXISTS (SELECT 1 FROM claims c2 WHERE c2.edge_id = cand.subject_id AND {vis})
            )
            """
        )

    def count_visible_edges_for_collections(self, caller, corpus_ids: List[str]) -> Dict[str, int]:
        """Caller-scoped edge count per collection — added alongside
        :meth:`count_visible_facts_for_collections` for the source card's
        pipeline-strip "edges" number (spec §13.2); there was no edge
        equivalent of that fact counter yet. Same batch-resolves-readable-
        once shape."""
        readable = _readable_ids(caller)
        is_admin = readable is None
        tiered_hidden, audience_pairs = _audience_context(caller, readable)
        base: Dict[str, Any] = (
            {}
            if is_admin
            else {"readable": list(readable), "tiered_hidden": tiered_hidden, "audience_pairs": audience_pairs}
        )
        sql = sa.text(f"WITH {self._visible_edges_for_corpus_cte(is_admin)} SELECT COUNT(*) FROM edge_visible")

        out: Dict[str, int] = {}
        if not corpus_ids:
            return out
        with self._engine.connect() as conn:
            for corpus_id in corpus_ids:
                row = conn.execute(sql, {**base, "corpus_id": corpus_id}).first()
                out[corpus_id] = int(row[0]) if row else 0
        return out

    def approximate_counts_for_collections(self, corpus_ids: List[str]) -> Dict[str, Dict[str, int]]:
        """``{corpus_id: {"facts": n, "edges": m}}`` for every given corpus,
        in ONE statement — an APPROXIMATE, unfiltered sibling of
        :meth:`count_visible_facts_for_collections`/`count_visible_edges_
        for_collections` for a caller that needs a cheap "how big is this"
        number, not a correct-for-this-caller one (incident, 2026-09-03).

        Those two methods are correctly one query PER collection each — the
        caller's readable set is resolved once, but the withheld/revealed-
        correction-aware, audience-gated visibility CTE genuinely cannot be
        collapsed across collections without a much larger, security-
        sensitive rewrite (candidacy and visibility interact per-claim, and
        endpoint evidence can draw from a DIFFERENT collection than
        candidacy — see :meth:`_visible_facts_for_corpus_cte`'s docstring).
        On a live instance with ~390 collections that cost 250-316s for a
        SINGLE admin page load's worth of connections (a `pg_stat_activity`
        sample showed one of these CTEs as the entire active query load),
        which starved the shared Postgres connection pool for minutes at a
        time regardless of how well the request itself was dispatched to
        the thread pool — unusable for a per-page-view read on a large
        corpus.

        This is a flat `COUNT(DISTINCT ...) ... GROUP BY corpus_id` over
        `claims` — one indexed (`idx_claims_corpus_id`) scan, no
        `corrections`/`edges`-join subqueries, no per-caller resolution.
        The ONLY thing it does not account for is a `wrong`/`restricted`
        correction (which withholds a fact/edge for EVERY caller, admin
        included) — a small, typically-empty set relative to the corpus,
        so the caller (`facts_graph_counts`, admin-only — every caller of
        THIS repo method already sees `_readable_ids(caller) is None`, i.e.
        no RBAC narrowing applies to begin with) must label the result
        `graph_counts_kind: "approximate"` rather than present it as the
        row-visibility-filtered number the other two methods return.
        `statement_timeout` is set for this call alone so a pathological
        corpus_id list fails fast with a clear error instead of repeating
        the incident.

        **TCRD-296 E.21**: reads `fact_collection_stats.facts_count`/
        `edges_count` directly — an EXACT match for what the old `GROUP BY`
        computed (both count every fact/edge with >=1 claim in the corpus,
        `wrong`/`restricted` included either way), just a primary-key lookup
        per corpus instead of a scan over every claim in it. Falls back to
        the original query, unconditionally, whenever `fact_collection_
        stats` is still empty — see `rebuild_collection_stats`'s docstring.
        """
        out: Dict[str, Dict[str, int]] = {}
        if not corpus_ids:
            return out
        sql = sa.text(
            """
            WITH from_stats AS (
                SELECT corpus_id, facts_count AS facts, edges_count AS edges
                FROM fact_collection_stats
                WHERE corpus_id = ANY(:ids) AND EXISTS (SELECT 1 FROM fact_collection_stats)
            ),
            from_claims AS (
                SELECT corpus_id, COUNT(DISTINCT fact_id) AS facts, COUNT(DISTINCT edge_id) AS edges
                FROM claims
                WHERE corpus_id = ANY(:ids) AND NOT EXISTS (SELECT 1 FROM fact_collection_stats)
                GROUP BY corpus_id
            )
            SELECT * FROM from_stats
            UNION ALL
            SELECT * FROM from_claims
            """
        )
        with self._engine.begin() as conn:
            conn.execute(sa.text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))
            rows = conn.execute(sql, {"ids": list(corpus_ids)}).mappings().all()
        for r in rows:
            out[r["corpus_id"]] = {"facts": int(r["facts"]), "edges": int(r["edges"])}
        return out

    def claims_count_by_file(self, file_ids: List[str]) -> Dict[str, int]:
        """``{corpus_file_id: count}`` for every id in ``file_ids`` that has
        AT LEAST ONE claim — an id with zero claims is simply absent (never
        a zero-valued entry), so a caller reads ``.get(file_id, 0)``.

        Used by the SharePoint facts ledger's reset-no-claims recovery
        (TCRD-296 gap #62): the ledger itself only ever records ``nodes``/
        ``edges`` counts (:func:`connectors.sharepoint.facts_extraction
        ._fold_accepted_result`), never ``claims_written`` — so a caller
        needing to tell "this file's own evidence was accepted" from "the
        extraction ran but nothing landed" cannot answer that from the
        ledger alone and must ask the fact graph directly.
        """
        out: Dict[str, int] = {}
        if not file_ids:
            return out
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT corpus_file_id, COUNT(*) AS n FROM claims WHERE corpus_file_id = ANY(:ids) GROUP BY corpus_file_id"
                    ),
                    {"ids": list(set(file_ids))},
                )
                .mappings()
                .all()
            )
        for r in rows:
            out[r["corpus_file_id"]] = int(r["n"])
        return out

    def corpus_facts_version(self, corpus_id: str) -> int:
        """Current write-version for ``corpus_id`` (see the module-level
        registry above `FactNotFound`) — 0 if this process has never
        bumped it. A cheap in-memory read, no DB round trip;
        `app/web/router.py`'s `collection_facts_summary` TTL cache folds
        this into its cache key so a write through this repository is
        visible on the very next read regardless of the cache's TTL."""
        with _corpus_facts_version_lock:
            return _corpus_facts_version.get(corpus_id, 0)

    def invalidate_corpus_facts_cache(self, corpus_id: str) -> None:
        """Escape hatch for a caller that changed ``corpus_id``'s
        claims/edges/corrections OUTSIDE this repository (raw SQL — a
        migration data fix, a test fixture) and needs the next
        `collection_facts_summary` read to reflect it immediately, the
        same way a write made THROUGH this repository already does."""
        _bump_corpus_facts_version(corpus_id)

    def _maintained_type_counts_for_collection(self, conn: Connection, corpus_id: str) -> Optional[Dict[str, int]]:
        """Read `fact_collection_type_counts` for ONE collection under the
        caller's own open transaction — TCRD-296 gap #81's per-type
        companion to `fact_collection_stats`, maintained by the SAME three
        writers (`_bump_collection_stats_impl`, `_decrement_collection_
        stats_impl`, `_rebuild_one_collection_stats`). Called from
        :meth:`collection_facts_summary` for an unrestricted (admin) caller
        only — this table cannot express per-caller visibility, so a
        non-admin caller always takes the exact CTE path regardless of what
        this returns.

        Returns ``None`` — never an empty dict — until THIS table
        specifically has been populated anywhere. Deliberately its OWN
        bootstrap gate, not the sibling `fact_collection_stats`'s: this
        table shipped in a LATER migration (0110) than the sibling (0105),
        so an instance already running with `fact_collection_stats`
        populated has a non-empty sibling but an EMPTY `fact_collection_
        type_counts` until the next `rebuild_collection_stats` run —
        borrowing the sibling's gate would misread that transient window as
        "this collection genuinely has no facts". A genuinely empty
        collection on an already-populated system correctly returns
        ``{}``, which the caller must NOT treat as "fall back to the exact
        query"."""
        populated = conn.execute(sa.text("SELECT EXISTS (SELECT 1 FROM fact_collection_type_counts)")).scalar()
        if not populated:
            return None
        rows = (
            conn.execute(
                sa.text("SELECT type, count FROM fact_collection_type_counts WHERE corpus_id = :corpus_id"),
                {"corpus_id": corpus_id},
            )
            .mappings()
            .all()
        )
        return {r["type"]: int(r["count"]) for r in rows}

    def collection_facts_summary(self, caller, corpus_id: str, *, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        """Caller-scoped facts section for one collection's detail page
        (spec §13.2 "Collection detail"): fact count by type, a paged list of
        facts evidenced by ``corpus_id`` (display name from the first
        natural-key alias, GLOBAL claim/quote counts matching `search()`'s
        contract so the same subject reads the same numbers everywhere), a
        same-key attribute conflict rendered INLINE on the fact that carries
        it (never a detached queue — every conflicting value with its
        document name/date, from the caller's own readable claims), and
        `possible_duplicate_of` review-item edges whose own claim AND both
        endpoints are independently visible to the caller (the same rule
        `neighbors()` applies to every edge — never inferred from an
        endpoint), and `single_valued_conflict` review items (spec §7.3) for
        a functionally single-valued edge type (`facts.single_valued_edges`)
        whose src fact carries >1 distinct dst with an independently
        visible claim — every item in ``review_items`` carries a ``kind``
        discriminator so the two shapes coexist in one list.

        Honesty note on "conflict": this is a SIMPLER definition than
        `search()`'s attrs projection (which shows a conflict only when the
        LATEST document_date ties across values) — here, every distinct
        value a caller can read for one attribute key is a conflict, because
        this surface's job is showing a reader the disagreement, not picking
        a winner. It intentionally surfaces more than the projection would.

        **TCRD-296 gap #78.** Used to run FOUR separate statements against
        `_visible_facts_for_corpus_cte` — one each for the type breakdown,
        the page, the `possible_duplicate_of` candidates and the
        single-valued-conflict candidates — every one re-deriving the SAME
        per-caller candidate set from scratch (production measurement,
        2026-09-04: ~1s per statement on a 552k-fact/2.4M-claim collection,
        called on this route TWICE per render). A non-recursive CTE
        referenced more than once in ONE statement is materialized by
        Postgres exactly once and shared by every reference (proven in
        `tests/db_pg/test_fact_collection_stats_pg.py`'s admin-fast-path
        EXPLAIN assertions), so the four are now downstream CTEs of the
        SAME `{cte}` inside a SINGLE combined statement below — `visible`/
        `candidates` is derived once per call, not four times, whichever
        branch of `_visible_facts_for_corpus_cte` (the indexed admin fast
        path or the full audience-gated one) applies. `total` additionally
        prefers `approximate_counts_for_collections` for an admin caller —
        the same O(1) `fact_collection_stats` lookup the Library index
        already uses (gap #70's "admin sees all") — falling back to the
        exact `sum(type_counts)` whenever the corpus has no stats row yet;
        the two can differ by the same small `wrong`/`restricted` margin
        `approximate_counts_for_collections`'s own docstring already
        documents.

        **TCRD-296 gap #81** (production measurement, 2026-09-05: 4.5-4.8s
        on a 291k-file/801k-fact collection — a `Parallel Seq Scan` of the
        whole `facts` table, 837k rows, hash-joined to `fact_collection_
        membership`, 801k rows, to produce a 27-row `type_counts`). For the
        SAME unrestricted-caller population `total` already fast-paths, the
        breakdown itself now comes from the maintained `fact_collection_
        type_counts` companion table (:meth:`_maintained_type_counts_for_
        collection`) instead of `type_counts_cte`'s `visible JOIN facts
        GROUP BY f.type` — which is skipped entirely (a `WHERE FALSE` stub)
        when the fast path applies, so this collection's summary no longer
        touches `facts` at all. A non-admin caller's breakdown is
        UNCHANGED — still the exact CTE below — because per-caller
        visibility (RBAC narrowing, audience tiers, `all_evidence` mode) is
        exactly what this maintained table cannot express; see that
        method's docstring for the bootstrap-gate reasoning. The
        `dup_edges_cte` OR (`e.src IN (...) OR e.dst IN (...)`) is rewritten
        as a UNION of two joins for the same reason: Postgres compiles each
        `IN (subquery)` disjunct as its OWN hashed SubPlan, so the OR forces
        materializing/hashing the (potentially huge) `visible` candidate set
        TWICE for 50 output rows, and — worse, with non-sequential ids —
        can degrade into an `edges_pkey` order scan that visits nearly the
        whole `edges` table chasing the `ORDER BY e.id LIMIT 50`. A UNION of
        two real JOINs lets the planner build its hash from the (usually
        tiny) `type = 'possible_duplicate_of'`-filtered side instead.
        """
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        readable = _readable_ids(caller)
        is_admin = readable is None
        all_evidence = _visibility_mode() == "all_evidence"
        tiered_hidden, audience_pairs = _audience_context(caller, readable)
        vis_params: Dict[str, Any] = {"corpus_id": corpus_id}
        if not is_admin:
            vis_params["readable"] = list(readable)
            vis_params["tiered_hidden"] = tiered_hidden
            vis_params["audience_pairs"] = audience_pairs
        cte = self._visible_facts_for_corpus_cte(is_admin, all_evidence)
        sv_types = list(_single_valued_edge_types())

        with self._engine.begin() as conn:
            conn.execute(sa.text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))

            # Gap #79: resolved INSIDE this same transaction so the maintained
            # table's own bootstrap-populated check and the combined query
            # below see a consistent snapshot.
            maintained_type_counts = self._maintained_type_counts_for_collection(conn, corpus_id) if is_admin else None
            if maintained_type_counts is not None:
                # The facts JOIN is skipped entirely — `type_counts` is
                # filled in from `maintained_type_counts` below instead of
                # `combined_row["type_counts"]`.
                type_counts_leg = "type_counts_cte AS (SELECT NULL::text AS type, NULL::bigint AS n WHERE FALSE),"
            else:
                type_counts_leg = (
                    "type_counts_cte AS (\n"
                    "                SELECT f.type AS type, COUNT(*) AS n\n"
                    "                FROM visible v JOIN facts f ON f.id = v.subject_id\n"
                    "                GROUP BY f.type\n"
                    "            ),"
                )

            # One round trip for everything `visible`-derived. Each of the four
            # pieces below is its own CTE off the shared `visible`/`candidates`
            # chain, JSON-aggregated into one row so `visible` is computed
            # exactly once regardless of how many of the four read it. Order is
            # applied INSIDE each `json_agg` (never relied on from scan order),
            # so a parallel or reordered plan can never scramble a section.
            combined_sql = sa.text(
                f"""
            WITH {cte},
            {type_counts_leg}
            page_cte AS (
                SELECT v.subject_id AS subject_id, v.is_revealed AS is_revealed, f.type AS type
                FROM visible v JOIN facts f ON f.id = v.subject_id
                ORDER BY f.type, v.subject_id
                LIMIT :limit_plus_one OFFSET :offset
            ),
            dup_edges_cte AS (
                SELECT id, src, dst FROM (
                    SELECT e.id AS id, e.src AS src, e.dst AS dst
                    FROM edges e
                    JOIN visible v ON v.subject_id = e.src
                    WHERE e.type = 'possible_duplicate_of'
                    UNION
                    SELECT e.id AS id, e.src AS src, e.dst AS dst
                    FROM edges e
                    JOIN visible v ON v.subject_id = e.dst
                    WHERE e.type = 'possible_duplicate_of'
                ) matched
                ORDER BY id
                LIMIT 50
            ),
            sv_candidates_cte AS (
                SELECT e.src AS src, e.type AS type
                FROM edges e
                WHERE e.type = ANY(:sv_types)
                  AND e.src IN (SELECT subject_id FROM visible)
                  AND EXISTS (SELECT 1 FROM claims c WHERE c.edge_id = e.id)
                GROUP BY e.src, e.type
                HAVING COUNT(DISTINCT e.dst) > 1
                ORDER BY e.src, e.type
                LIMIT 50
            )
            SELECT
                COALESCE(
                    (SELECT json_agg(json_build_object('type', type, 'n', n) ORDER BY type) FROM type_counts_cte),
                    '[]'
                ) AS type_counts,
                COALESCE(
                    (SELECT json_agg(
                        json_build_object('subject_id', subject_id, 'is_revealed', is_revealed, 'type', type)
                        ORDER BY type, subject_id
                    ) FROM page_cte),
                    '[]'
                ) AS page,
                COALESCE(
                    (SELECT json_agg(json_build_object('id', id, 'src', src, 'dst', dst) ORDER BY id)
                     FROM dup_edges_cte),
                    '[]'
                ) AS dup_edges,
                COALESCE(
                    (SELECT json_agg(json_build_object('src', src, 'type', type) ORDER BY src, type)
                     FROM sv_candidates_cte),
                    '[]'
                ) AS sv_candidates
            """
            )
            combined_params = dict(vis_params)
            combined_params["limit_plus_one"] = limit + 1
            combined_params["offset"] = offset
            combined_params["sv_types"] = sv_types

            combined_row = conn.execute(combined_sql, combined_params).mappings().first()
            assert combined_row is not None

            if maintained_type_counts is not None:
                type_counts = maintained_type_counts
            else:
                type_counts = {r["type"]: int(r["n"]) for r in (_decode_jsonb(combined_row["type_counts"]) or [])}

            page_rows = _decode_jsonb(combined_row["page"]) or []
            limit_applied = len(page_rows) > limit
            page_rows = page_rows[:limit]
            page_ids = [r["subject_id"] for r in page_rows]
            revealed_by_id = {r["subject_id"]: bool(r["is_revealed"]) for r in page_rows}
            type_by_id = {r["subject_id"]: r["type"] for r in page_rows}

            facts_out: List[Dict[str, Any]] = []
            review_items: List[Dict[str, Any]] = []

            if page_ids:
                # Same alias-visibility rule as search()/neighbors() (security
                # hardening): a page fact being visible here doesn't make
                # every one of its aliases readable — only one whose
                # provenance corpus the caller can read (or a revealed
                # subject, bypassing grants per spec §4) may become its
                # display name. Falls back to the opaque fact id below.
                alias_readable = self._alias_readable_sql(
                    revealed_expr="fa.fact_id = ANY(:revealed_page_ids)", is_admin=is_admin
                )
                alias_sql = sa.text(
                    f"""
                    SELECT fa.fact_id, fa.natural_key
                    FROM fact_aliases fa
                    WHERE fa.fact_id = ANY(:ids)
                      AND {alias_readable}
                    ORDER BY fa.fact_id, fa.natural_key
                    """
                )
                alias_params: Dict[str, Any] = {
                    "ids": page_ids,
                    "revealed_page_ids": [fid for fid, rev in revealed_by_id.items() if rev],
                }
                if not is_admin:
                    alias_params["readable"] = list(readable)
                alias_rows = conn.execute(alias_sql, alias_params).mappings().all()
                display_name: Dict[str, str] = {}
                for r in alias_rows:
                    display_name.setdefault(r["fact_id"], r["natural_key"])

                # GLOBAL readable claims for the page's subjects — same rule
                # search()'s counted_claims applies: a revealed subject counts
                # every claim regardless of grants (quotes suppressed instead,
                # not the count), everyone else counts only grant-visible ones.
                claim_vis = self._visibility_predicate("c.corpus_id", is_admin)
                revealed_ids = [fid for fid, rev in revealed_by_id.items() if rev]
                claims_sql = sa.text(
                    f"""
                    SELECT c.fact_id, c.attrs, c.document_date, cf.filename
                    FROM claims c
                    JOIN corpus_files cf ON cf.id = c.corpus_file_id
                    WHERE c.fact_id = ANY(:ids) AND (c.fact_id = ANY(:revealed_ids) OR ({claim_vis}))
                    """
                )
                claims_params: Dict[str, Any] = {"ids": page_ids, "revealed_ids": revealed_ids}
                if not is_admin:
                    claims_params["readable"] = list(readable)
                    claims_params["tiered_hidden"] = tiered_hidden
                    claims_params["audience_pairs"] = audience_pairs
                claim_rows = conn.execute(claims_sql, claims_params).mappings().all()

                claim_count: Dict[str, int] = {}
                # subject_id -> attr key -> value's json key -> {"value", "docs": {(name, date), ...}}
                attr_values: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
                for r in claim_rows:
                    fid = r["fact_id"]
                    claim_count[fid] = claim_count.get(fid, 0) + 1
                    attrs = _decode_jsonb(r["attrs"]) or {}
                    doc_date = r["document_date"].isoformat() if r["document_date"] else None
                    for key, value in attrs.items():
                        value_key = json.dumps(value, sort_keys=True)
                        bucket = attr_values.setdefault(fid, {}).setdefault(key, {})
                        entry = bucket.setdefault(value_key, {"value": value, "docs": set()})
                        entry["docs"].add((r["filename"], doc_date))

                for fid in page_ids:
                    is_revealed = revealed_by_id.get(fid, False)
                    n_claims = claim_count.get(fid, 0)
                    conflicts = []
                    for key, values in (attr_values.get(fid) or {}).items():
                        if len(values) < 2:
                            continue
                        entries = []
                        for entry in values.values():
                            for doc_name, doc_date in sorted(entry["docs"], key=lambda t: (t[0] or "", t[1] or "")):
                                entries.append(
                                    {"value": entry["value"], "document_name": doc_name, "document_date": doc_date}
                                )
                        conflicts.append({"key": key, "entries": entries})
                    facts_out.append(
                        {
                            "id": fid,
                            "type": type_by_id.get(fid),
                            "display_name": display_name.get(fid, fid),
                            "claim_count": n_claims,
                            "quote_count": 0 if is_revealed else n_claims,
                            "revealed": is_revealed,
                            "conflicts": conflicts,
                        }
                    )

                dup_edge_rows = _decode_jsonb(combined_row["dup_edges"]) or []
                review_items = self._review_items_for_corpus(
                    conn,
                    edge_rows=dup_edge_rows,
                    is_admin=is_admin,
                    all_evidence=all_evidence,
                    readable=readable,
                    tiered_hidden=tiered_hidden,
                    audience_pairs=audience_pairs,
                )
                sv_candidate_rows = _decode_jsonb(combined_row["sv_candidates"]) or []
                review_items += self._single_valued_review_items_for_corpus(
                    conn,
                    candidates=sv_candidate_rows,
                    is_admin=is_admin,
                    all_evidence=all_evidence,
                    readable=readable,
                    tiered_hidden=tiered_hidden,
                    audience_pairs=audience_pairs,
                )

        total = sum(type_counts.values())
        if is_admin and maintained_type_counts is None:
            # Gap #78's other half: on a corpus large enough for the exact
            # breakdown above to matter, the O(1) `fact_collection_stats`
            # lookup the Library index already trusts for "how big is this"
            # (`approximate_counts_for_collections`, gap #70) is a strictly
            # cheaper number to page against than one that took a full scan
            # to produce — same "admin sees all" scope, no per-caller
            # narrowing lost. Skipped when `maintained_type_counts` already
            # applied (gap #81): `sum(maintained_type_counts.values())` is
            # additively the SAME number `fact_collection_stats.facts_count`
            # holds (both maintained by the identical writers), so a second
            # round trip here would be redundant. Left as `sum(type_counts)`
            # whenever the corpus has no stats row yet (pre-rebuild, or a
            # corpus with zero facts).
            approx = self.approximate_counts_for_collections([corpus_id])
            total = approx.get(corpus_id, {}).get("facts", total)

        return {
            "total": total,
            "type_counts": type_counts,
            "facts": facts_out,
            "limit_applied": limit_applied,
            "review_items": review_items,
        }

    def _review_items_for_corpus(
        self,
        conn,
        *,
        edge_rows: List[Mapping[str, Any]],
        is_admin: bool,
        all_evidence: bool,
        readable: Optional[frozenset],
        tiered_hidden: Optional[List[str]] = None,
        audience_pairs: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """`possible_duplicate_of` edges touching a fact evidenced by the
        caller's collection — spec §7.2's entity-resolution review items,
        surfaced as rows with both subjects named rather than a detached
        queue. Every edge is independently visibility-checked (its OWN
        claim, never inferred from its endpoints — the same rule S3/S4 pin
        for `neighbors()`), and both endpoints must be independently
        visible too, so this can never announce a fact the caller cannot
        otherwise see.

        ``edge_rows`` — ``{"id", "src", "dst"}`` mappings, already capped at
        50 — is pre-fetched by the caller (TCRD-296 gap #78:
        `collection_facts_summary`'s combined statement now runs this
        candidate query as one of its CTEs, so this method only does the
        per-edge visibility walk below — never a second, redundant
        derivation of the same candidate set)."""
        out: List[Dict[str, Any]] = []
        for erow in edge_rows:
            eid, src, dst = erow["id"], erow["src"], erow["dst"]
            edge_status = self._subject_status(
                conn,
                subject_kind="edge",
                subject_id=eid,
                is_admin=is_admin,
                all_evidence=all_evidence,
                readable=readable,
                tiered_hidden=tiered_hidden,
                audience_pairs=audience_pairs,
            )
            if not self._is_visible(edge_status):
                continue
            a_status = self._subject_status(
                conn,
                subject_kind="fact",
                subject_id=src,
                is_admin=is_admin,
                all_evidence=all_evidence,
                readable=readable,
                tiered_hidden=tiered_hidden,
                audience_pairs=audience_pairs,
            )
            b_status = self._subject_status(
                conn,
                subject_kind="fact",
                subject_id=dst,
                is_admin=is_admin,
                all_evidence=all_evidence,
                readable=readable,
                tiered_hidden=tiered_hidden,
                audience_pairs=audience_pairs,
            )
            if not (self._is_visible(a_status) and self._is_visible(b_status)):
                continue
            out.append(
                {
                    "kind": "possible_duplicate_of",
                    "edge_id": eid,
                    "a": self._subject_label(
                        conn, src, is_admin=is_admin, readable=readable, revealed=bool(a_status["revealed"])
                    ),
                    "b": self._subject_label(
                        conn, dst, is_admin=is_admin, readable=readable, revealed=bool(b_status["revealed"])
                    ),
                }
            )
        return out

    def _single_valued_review_items_for_corpus(
        self,
        conn,
        *,
        candidates: List[Mapping[str, Any]],
        is_admin: bool,
        all_evidence: bool,
        readable: Optional[frozenset],
        tiered_hidden: Optional[List[str]] = None,
        audience_pairs: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Functionally single-valued edges (spec §7.3) whose src fact is
        evidenced by the caller's collection: >1 distinct dst, each carrying
        its OWN independently-visible claim (the exact discipline
        `_review_items_for_corpus` applies to `possible_duplicate_of` — an
        edge's own claim AND its dst endpoint must each pass
        `_subject_status`/`_is_visible`), for an edge type this instance
        configured as single-valued (`facts.single_valued_edges`). A dst
        the caller cannot independently see is dropped from the group
        rather than hiding the whole conflict — but if that drops the
        group back to <=1 visible dst, the item disappears entirely (never
        announces a conflict whose second edge the caller cannot read).
        Recomputed on every call from live edges/claims, nothing persisted,
        so it clears the moment a dst's claims are gone.

        ``candidates`` — ``{"src", "type"}`` mappings, RAW distinct-dst
        count >1 among live (>=1 claim) edges, already capped at 50 — is
        pre-fetched by the caller, the sibling of `_review_items_for_corpus`'s
        ``edge_rows`` (TCRD-296 gap #78: same "candidate query moved into
        `collection_facts_summary`'s combined statement" change). An empty
        ``candidates`` list (no `facts.single_valued_edges` configured, or
        none evidenced here) is simply a no-op loop, same result as the old
        early ``if not types: return []``."""
        out: List[Dict[str, Any]] = []
        for cand in candidates:
            src, etype = cand["src"], cand["type"]
            src_status = self._subject_status(
                conn,
                subject_kind="fact",
                subject_id=src,
                is_admin=is_admin,
                all_evidence=all_evidence,
                readable=readable,
                tiered_hidden=tiered_hidden,
                audience_pairs=audience_pairs,
            )
            edge_rows = (
                conn.execute(
                    sa.text("SELECT id, dst FROM edges WHERE src = :src AND type = :type"),
                    {"src": src, "type": etype},
                )
                .mappings()
                .all()
            )
            visible_dsts: Dict[str, bool] = {}
            for erow in edge_rows:
                eid, dst = erow["id"], erow["dst"]
                edge_status = self._subject_status(
                    conn,
                    subject_kind="edge",
                    subject_id=eid,
                    is_admin=is_admin,
                    all_evidence=all_evidence,
                    readable=readable,
                    tiered_hidden=tiered_hidden,
                    audience_pairs=audience_pairs,
                )
                if not self._is_visible(edge_status):
                    continue
                dst_status = self._subject_status(
                    conn,
                    subject_kind="fact",
                    subject_id=dst,
                    is_admin=is_admin,
                    all_evidence=all_evidence,
                    readable=readable,
                    tiered_hidden=tiered_hidden,
                    audience_pairs=audience_pairs,
                )
                if not self._is_visible(dst_status):
                    continue
                visible_dsts[dst] = bool(dst_status["revealed"])
            if len(visible_dsts) < 2:
                continue
            out.append(
                {
                    "kind": "single_valued_conflict",
                    "type": etype,
                    "src": self._subject_label(
                        conn, src, is_admin=is_admin, readable=readable, revealed=bool(src_status["revealed"])
                    ),
                    "dsts": [
                        self._subject_label(conn, dst, is_admin=is_admin, readable=readable, revealed=rev)
                        for dst, rev in sorted(visible_dsts.items())
                    ],
                }
            )
        return out

    def _subject_label(
        self, conn, fact_id: str, *, is_admin: bool, readable: Optional[frozenset], revealed: bool = False
    ) -> Dict[str, Any]:
        """Same alias-visibility rule as ``search()``/``neighbors()``
        (security hardening): a subject already confirmed visible to this
        caller still only shows an alias whose OWN provenance corpus the
        caller can read (or unconditionally, when ``revealed`` — spec §4).
        Falls back to the opaque ``fact_id`` when no alias qualifies, same
        as an admin-only-visible alias everywhere else in this module."""
        row = conn.execute(sa.text("SELECT id, type FROM facts WHERE id = :id"), {"id": fact_id}).mappings().first()
        alias_readable = self._alias_readable_sql(revealed_expr=":revealed", is_admin=is_admin)
        alias_params: Dict[str, Any] = {"id": fact_id, "revealed": revealed}
        if not is_admin:
            alias_params["readable"] = list(readable) if readable else []
        alias_row = (
            conn.execute(
                sa.text(
                    "SELECT fa.natural_key FROM fact_aliases fa WHERE fa.fact_id = :id "
                    f"AND ({alias_readable}) "
                    "ORDER BY fa.natural_key LIMIT 1"
                ),
                alias_params,
            )
            .mappings()
            .first()
        )
        return {
            "id": fact_id,
            "type": row["type"] if row else None,
            "display_name": (alias_row["natural_key"] if alias_row else fact_id),
        }

    # ------------------------------------------------------------------
    # orphan sweep (spec §6) — deletes zero-claim subjects, counts them.
    # ------------------------------------------------------------------

    def sweep_orphans(self, *, grace_seconds: int = _ORPHAN_SWEEP_GRACE_S) -> Dict[str, Any]:
        """Delete every orphaned subject (fact or edge); return
        ``{"deleted": <int>, "skipped": <bool>}`` (spec §6, refined per the
        module docstring's "Endpoint evidence": an edge anchors its
        endpoints, so a fact is orphaned only when it has NEITHER an own
        claim NOR an incident edge carrying any claim). Correction-agnostic
        throughout — this is raw data hygiene, not a visibility check; a
        `wrong`/`restricted` edge with a live claim still anchors its
        endpoints here exactly like any other edge.

        Edges are swept FIRST: an edge with zero claims of its own is
        removed before the fact sweep runs — with ONE exception.
        ``possible_duplicate_of`` is claimless BY DESIGN: it is a proposal
        about two facts, not an assertion a document made, and neither a
        producer's own proposal nor an automatic candidate carries evidence.
        Sweeping it as an orphan deleted every candidate in the same
        ``ingest_batch`` that had just minted it, so the review queue never
        received one and the feature produced nothing (Devin Review on
        #2075). It is therefore excluded from the edge sweep, and — the half
        that is easy to miss — from the fact sweep's "is anything incident"
        test too: a claimless edge must not anchor an unevidenced fact, or a
        proposal about two garbage-collected facts would keep both alive
        forever. A candidate whose endpoint is swept dies with it through the
        existing ``ON DELETE CASCADE`` on ``src``/``dst``.

        With that exception the ordering argument still holds: by the time
        the fact DELETE runs, every surviving edge either carries >=1 claim
        or is a proposal that does not anchor anything, so "zero incident
        edges carrying any claims" collapses to "zero incident edges that
        are not proposals". Deleting an
        orphaned FACT afterwards can then cascade (``ON DELETE CASCADE``)
        any edge still pointing at it even if THAT edge carried its own
        claims — an edge to a subject this design has garbage-collected
        cannot outlive it, so that knock-on cascade is not separately
        counted here. Safe to call unconditionally (a no-op when nothing is
        orphaned); callers decide when running it is warranted (post-ingest
        — replace mode can orphan a subject a document no longer mentions —
        and post file delete, spec §6's lifecycle table).

        **Concurrency (live finding, 2026-09 — see
        migrations/versions/0100_facts_created_at.py): two guards close the
        race between several facts-extraction passes each ending their own
        `ingest_batch` with this sweep:**

        1. **Grace period** — a FACT younger than ``grace_seconds`` (default
           :data:`_ORPHAN_SWEEP_GRACE_S`, 15 minutes; NULL ``created_at``, a
           row written before this column existed, reads as "old enough") is
           never swept no matter how orphaned it looks right now: a fact
           minted moments ago purely as an edge endpoint (``_endpoint()``'s
           fallback, its own transaction — see :class:`EdgeEndpointMissing`)
           or a node whose own evidence is still deferred/rejected commits
           with zero claims for a real, if brief, window before the write
           that gives it its claims. ``grace_seconds=0`` recovers the
           pre-existing immediate-delete behavior for a caller that knows no
           other pass could be concurrently orphaning the same subject (a
           test; the collections file-delete hook). Edges need no such
           guard — an edge only ever commits already carrying its final
           claim set for its batch (or legitimately zero, the exempted
           proposal case above), never a transiently-incomplete one.
        2. **Serialized sweeps** — a transaction-scoped Postgres advisory
           lock (:data:`_SWEEP_LOCK_ID`) means at most one pass's sweep
           actually touches the database at a time. A pass that cannot
           acquire it skips its OWN sweep rather than waiting — the next
           pass sweeps instead — and returns ``{"deleted": 0, "skipped":
           True}`` without running either DELETE.
        """
        with self._engine.begin() as conn:
            acquired = bool(
                conn.execute(sa.text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _SWEEP_LOCK_ID}).scalar()
            )
            if not acquired:
                return {"deleted": 0, "skipped": True}
            edge_ids = (
                conn.execute(
                    sa.text(
                        "DELETE FROM edges e WHERE e.type <> 'possible_duplicate_of' "
                        "AND NOT EXISTS "
                        "(SELECT 1 FROM claims c WHERE c.edge_id = e.id) RETURNING e.id"
                    )
                )
                .scalars()
                .all()
            )
            fact_ids = (
                conn.execute(
                    sa.text(
                        "DELETE FROM facts f WHERE NOT EXISTS "
                        "(SELECT 1 FROM claims c WHERE c.fact_id = f.id) "
                        "AND NOT EXISTS (SELECT 1 FROM edges e "
                        "WHERE (e.src = f.id OR e.dst = f.id) "
                        "AND e.type <> 'possible_duplicate_of') "
                        "AND (f.created_at IS NULL OR f.created_at <= now() - make_interval(secs => :grace_seconds)) "
                        "RETURNING f.id"
                    ),
                    {"grace_seconds": grace_seconds},
                )
                .scalars()
                .all()
            )
        return {"deleted": len(edge_ids) + len(fact_ids), "skipped": False}

    # ------------------------------------------------------------------
    # corrections management support (spec §3/§4) — natural-key snapshots
    # for admin CRUD, and re-attachment for a subject recreated after
    # deletion (never a producer-writable path; see app/api/facts.py).
    # ------------------------------------------------------------------

    def natural_keys_for(self, subject_kind: str, subject_id: str) -> Any:
        """Snapshot a LIVE subject's natural keys for ``corrections.
        natural_keys`` (spec §3): for a fact, ``{"aliases": [...]}`` (every
        alias currently pointing at it); for an edge, ``[src_key, type,
        dst_key]`` using the first alias found for each endpoint (sorted,
        so the choice is deterministic when an endpoint carries more than
        one). Best-effort — an endpoint with no alias yet yields ``None``
        in its slot rather than raising; the correction is still recorded,
        it just cannot re-attach through that endpoint later."""
        with self._engine.connect() as conn:
            if subject_kind == "fact":
                rows = (
                    conn.execute(
                        sa.text("SELECT natural_key FROM fact_aliases WHERE fact_id = :id ORDER BY natural_key"),
                        {"id": subject_id},
                    )
                    .scalars()
                    .all()
                )
                return {"aliases": list(rows)}
            edge = (
                conn.execute(sa.text("SELECT src, type, dst FROM edges WHERE id = :id"), {"id": subject_id})
                .mappings()
                .first()
            )
            if edge is None:
                return [None, None, None]
            src_key = conn.execute(
                sa.text("SELECT natural_key FROM fact_aliases WHERE fact_id = :id ORDER BY natural_key LIMIT 1"),
                {"id": edge["src"]},
            ).scalar()
            dst_key = conn.execute(
                sa.text("SELECT natural_key FROM fact_aliases WHERE fact_id = :id ORDER BY natural_key LIMIT 1"),
                {"id": edge["dst"]},
            ).scalar()
            return [src_key, edge["type"], dst_key]

    def _reattach_correction(
        self, conn, *, subject_kind: str, subject_id: str, natural_key_probe: Any
    ) -> Optional[Dict[str, Any]]:
        """A subject deleted (all claims cascaded away) and later re-created
        under a NEW surrogate id re-attaches any orphaned correction whose
        snapshot overlaps this subject's natural key (spec §3: "a legal
        hold must not vanish because a document was briefly missing").
        Matches only a correction whose OWN ``subject_id`` no longer names
        a LIVE subject — never steals a correction still attached to a
        different, currently-live subject. Returns the repointed row (for
        the ingest run report's ``corrections_active``) or ``None``."""
        if subject_kind == "fact":
            probe_sql = "natural_keys @> CAST(:probe AS JSONB)"
            probe = json.dumps({"aliases": [natural_key_probe]})
            live_table = "facts"
        else:
            probe_sql = "natural_keys = CAST(:probe AS JSONB)"
            probe = json.dumps(natural_key_probe)
            live_table = "edges"

        row = (
            conn.execute(
                sa.text(
                    f"SELECT subject_id, verdict, reason FROM corrections "
                    f"WHERE subject_kind = :kind AND {probe_sql} "
                    f"AND subject_id != :new_id "
                    f"AND NOT EXISTS (SELECT 1 FROM {live_table} WHERE id = corrections.subject_id) "
                    f"LIMIT 1"
                ),
                {"kind": subject_kind, "probe": probe, "new_id": subject_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        old_subject_id = row["subject_id"]
        conn.execute(
            sa.text("UPDATE corrections SET subject_id = :new_id WHERE subject_kind = :kind AND subject_id = :old_id"),
            {"new_id": subject_id, "kind": subject_kind, "old_id": old_subject_id},
        )
        return {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "verdict": row["verdict"],
            "reason": row["reason"],
        }

    def _propose_duplicate_candidates(
        self, conn, *, new_facts: List[Tuple[str, str, str]], already_proposed: set
    ) -> List[Dict[str, Any]]:
        """For each ``(node_id, fact_id, type)`` in ``new_facts`` — a fact
        THIS `ingest_batch` call just minted (``_resolve_alias``'s
        ``created=True`` path, which fires exactly once per fact's
        lifetime) — compare its natural key against every OTHER alias of
        the same type already in the graph, INCLUDING ones this same
        batch minted earlier, and write a `possible_duplicate_of` edge for
        every :func:`_duplicate_candidate_reason` hit, the same way a
        producer's own proposal is written (no evidence required). The
        edge surfaces through the SAME review-item mechanism a
        model-authored one does (`_review_items_for_corpus`,
        `collection_facts_summary`) — this is a second SOURCE for that
        edge type, not a new review surface, and it never merges anything
        on its own initiative.

        Fires only for facts THIS batch newly minted, so a pair already
        covered by an earlier ingest is never re-scanned (a re-crawled,
        unchanged document that only adds a claim to an already-existing
        fact triggers nothing here). ``already_proposed`` — an unordered
        ``frozenset({fact_id, fact_id})`` per pair, owned by the caller —
        exists only to stop THIS call proposing the same pair twice when
        a batch mints both halves of it (each half's scan would otherwise
        find the other)."""
        if not new_facts:
            return []
        by_type: Dict[str, List[Tuple[str, str]]] = {}
        for node_id, fact_id, type_ in new_facts:
            by_type.setdefault(type_, []).append((node_id, fact_id))

        proposed: List[Dict[str, Any]] = []
        for type_, entries in by_type.items():
            pool = (
                conn.execute(
                    sa.text("SELECT natural_key, fact_id FROM fact_aliases WHERE type = :type"), {"type": type_}
                )
                .mappings()
                .all()
            )
            for node_id, fact_id in entries:
                slug = node_id.split(":", 1)[1] if ":" in node_id else node_id
                matches: List[Tuple[str, str, str]] = []  # (other_node_id, other_fact_id, reason)
                for row in pool:
                    other_key, other_fact_id = row["natural_key"], row["fact_id"]
                    if other_fact_id == fact_id:
                        continue
                    other_slug = other_key.split(":", 1)[1] if ":" in other_key else other_key
                    reason = _duplicate_candidate_reason(slug, other_slug)
                    if reason:
                        matches.append((other_key, other_fact_id, reason))
                if not matches or len(matches) > self._MAX_DUPLICATE_CANDIDATES_PER_FACT:
                    continue
                for other_node_id, other_fact_id, reason in matches:
                    pair = frozenset((fact_id, other_fact_id))
                    if pair in already_proposed:
                        continue
                    already_proposed.add(pair)
                    edge_id = self.create_edge(src=fact_id, type="possible_duplicate_of", dst=other_fact_id)
                    proposed.append(
                        {
                            "type": "possible_duplicate_of",
                            "edge_id": edge_id,
                            "src": node_id,
                            "dst": other_node_id,
                            "auto": True,
                            "reason": reason,
                        }
                    )
        return proposed

    def _resolve_alias(self, conn, node_id: str, declared_type: Optional[str]) -> Dict[str, Any]:
        """Resolve a producer node id ``<type>:<slug>`` to a fact_id via
        ``fact_aliases`` (spec §7.2): unknown -> new subject + alias (with
        correction re-attachment, above); a TYPE disagreement against an
        EXISTING alias for the same natural key is rejected, itemized
        (``alias_type_conflict``) rather than hard-exiting like the
        producer's own sandbox loader.

        The returned ``type`` is the alias's own ``(type, natural_key)``
        key — always ``resolved_type`` (the existing branch already asserts
        it matches, above) — so the caller can register per-corpus alias
        provenance (:meth:`add_alias_source`) once this node's evidence is
        written, without re-deriving the type from the node id itself."""
        parts = node_id.split(":", 1) if node_id else []
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return {"fact_id": None, "type": None, "created": False, "error": "malformed_node_id", "reattached": None}
        prefix_type = parts[0]
        resolved_type = declared_type or prefix_type

        existing = (
            conn.execute(
                sa.text("SELECT fact_id, type FROM fact_aliases WHERE natural_key = :nk"),
                {"nk": node_id},
            )
            .mappings()
            .first()
        )
        if existing is not None:
            if existing["type"] != resolved_type:
                return {
                    "fact_id": None,
                    "type": None,
                    "created": False,
                    "error": "alias_type_conflict",
                    "reattached": None,
                }
            return {
                "fact_id": existing["fact_id"],
                "type": resolved_type,
                "created": False,
                "error": None,
                "reattached": None,
            }

        # `conn` is threaded through to `create_fact`/`_reattach_correction`
        # (rather than each opening its own transaction) so a NEWLY-minted
        # fact is never visible to a concurrent call's `sweep_orphans()` in
        # a zero-claims, zero-alias state — see `ingest_batch`'s per-node/
        # per-edge transaction and `EdgeEndpointMissing`'s docstring for the
        # race this closes.
        fact_id = self.create_fact(type=resolved_type, natural_key=node_id, conn=conn)
        reattached = self._reattach_correction(conn, subject_kind="fact", subject_id=fact_id, natural_key_probe=node_id)
        return {"fact_id": fact_id, "type": resolved_type, "created": True, "error": None, "reattached": reattached}

    # ------------------------------------------------------------------
    # ingest (spec §7.2, build order step 4) — the write path's single
    # entry point. Batch caps, doc_id resolution (§6), the verbatim gate
    # (§8), union/replace modes, alias/edge resolution, correction
    # re-attachment (§3), and the orphan sweep (§6) all happen here; the
    # return value IS the run report (§7.2's response shape).
    # ------------------------------------------------------------------

    def ingest_batch(
        self,
        *,
        documents: Optional[List[Dict[str, Any]]] = None,
        full_documents: Optional[List[str]] = None,
        nodes: Optional[List[Dict[str, Any]]] = None,
        edges: Optional[List[Dict[str, Any]]] = None,
        orphan_sweep_grace_seconds: int = _ORPHAN_SWEEP_GRACE_S,
        run_orphan_sweep: bool = True,
    ) -> Dict[str, Any]:
        """``orphan_sweep_grace_seconds`` — passed straight through to the
        end-of-batch :meth:`sweep_orphans` call (see its docstring's
        "Concurrency" section); no production caller overrides this, it
        exists so a test that depends on THIS SAME batch's sweep deleting a
        subject it just orphaned (e.g. `full_documents` replace mode
        dropping a stale claim) can request the pre-existing immediate-
        delete behavior with ``orphan_sweep_grace_seconds=0``.

        ``run_orphan_sweep`` (TCRD-296 C.12 — live finding, 2026-09: with
        every batch of a multi-batch pass ending in its OWN sweep, 7
        parallel SharePoint facts-extraction passes deleted 75,447 subjects
        against 10,784 created in 30 minutes — one pass's per-batch sweep
        kept catching a SIBLING pass's just-created, not-yet-evidenced
        subject before that pass's own later batch could attach its claim).
        A multi-batch caller (:func:`connectors.sharepoint.facts_extraction
        .run_facts_extraction`) sets this ``False`` for every batch and
        calls :meth:`sweep_orphans` itself exactly ONCE, after the whole
        pass has shipped — cutting the sweep's own contribution to that
        race by the batch count, on top of the grace period and advisory
        lock :meth:`sweep_orphans` already enforces. ``True`` (the default)
        preserves the pre-existing per-call sweep for every other caller —
        the HTTP route (:func:`app.api.facts.facts_ingest`, external
        producers included) and every existing test."""
        documents = documents or []
        full_documents = full_documents or []
        nodes = nodes or []
        edges = edges or []

        if len(documents) > MAX_INGEST_DOCUMENTS:
            raise IngestBatchTooLarge(
                {"reason": "too_many_documents", "count": len(documents), "cap": MAX_INGEST_DOCUMENTS}
            )

        # RESERVED SHAPE (security, not a format quirk) — checked BEFORE any
        # document in this batch is resolved or upserted, same reasoning as
        # the upload endpoint's identical guard
        # (`app.api.collections.upload_files`): a `stable_id` on the bundle-
        # member anchor shape must never reach the `documents[]` resolve/
        # upsert loop below, which would otherwise let it overwrite a real
        # member's `corpus_file_sources` row (see `IngestReservedStableId`).
        _reserved_stable_ids = sorted(
            {
                doc["stable_id"]
                for doc in documents
                if isinstance(doc.get("stable_id"), str) and is_reserved_member_stable_id(doc["stable_id"])
            }
        )
        if _reserved_stable_ids:
            raise IngestReservedStableId(_reserved_stable_ids)

        per_doc_claims: Dict[str, int] = {}
        for node in nodes:
            for ev in node.get("evidence") or []:
                doc_id = ev.get("doc_id")
                if doc_id:
                    per_doc_claims[doc_id] = per_doc_claims.get(doc_id, 0) + 1
        for edge in edges:
            for ev in edge.get("evidence") or []:
                doc_id = ev.get("doc_id")
                if doc_id:
                    per_doc_claims[doc_id] = per_doc_claims.get(doc_id, 0) + 1

        for doc_id, count in per_doc_claims.items():
            if count > MAX_INGEST_CLAIMS:
                raise IngestDocumentExceedsClaimCap(doc_id, count)

        total_claims = sum(per_doc_claims.values())
        if total_claims > MAX_INGEST_CLAIMS:
            raise IngestBatchTooLarge({"reason": "too_many_claims", "count": total_claims, "cap": MAX_INGEST_CLAIMS})

        from src.repositories import corpus_chunks_repo, corpus_file_sources_repo, corpus_files_repo

        cf_repo = corpus_files_repo()
        sources_repo = corpus_file_sources_repo()
        chunks_repo = corpus_chunks_repo()

        # ---- documents[]: upsert through the §6 path (match stable_id then
        # path, refresh the corpus_file_sources mapping only) — NEVER
        # creates a new corpus_files row. This flow's content already
        # landed through the normal upload endpoint (spec §7.2, "what the
        # producer uploads"); a document row with no matching corpus_files
        # row stays unresolved.
        #
        # TCRD-241: a byte-identical SharePoint copy shares its sha-derived
        # doc_id with every other copy, so `corpus_file_sources` can legally
        # hold MORE THAN ONE row for one `source_doc_id` — possibly across
        # different collections. `doc_id_resolution` is therefore keyed by
        # (corpus_id, doc_id), never plain doc_id, so a duplicate declared
        # under a second corpus in this same batch never silently overwrites
        # the first's resolution.
        doc_id_resolution: Dict[Tuple[str, str], str] = {}
        doc_declared_pairs: Set[Tuple[str, str]] = set()  # {(corpus_id, doc_id)} this batch's documents[] touched
        # RBAC review follow-up (P1): every corpus a `documents[]` entry
        # NAMED, regardless of whether that entry actually resolved to a
        # corpus_file_id. `doc_declared_pairs` alone under-counts this on a
        # rename race (stable_id/path match nothing yet, no prior
        # corpus_file_sources row either) — the entry silently fails to
        # resolve, so its corpus never lands in `doc_declared_pairs`, and a
        # batch with exactly one such entry would otherwise present as
        # "documents[] omitted" to `_resolve_doc` and fall through to its
        # unrestricted tier-3 scan. `declared_corpus_ids` is the batch's
        # true declared scope: the gate for tier 3 below.
        declared_corpus_ids: Set[str] = set()
        # doc_id -> this batch's own declared `modified` date. Keyed by
        # doc_id, NOT corpus_file_id (P2 review finding) — see the write
        # site below for why a file_id key silently drops the date on a
        # TCRD-241 duplicate-copy override.
        doc_dates: Dict[str, date] = {}
        # O7 follow-up: a `source_url` the validator dropped — itemized so a
        # non-zero count on the run report tells the operator "your producer
        # is sending urls Agnes won't store" instead of a citation silently
        # never getting a link. Only appended when the producer actually
        # SENT a value (see `_validate_source_url`'s reason contract) — an
        # absent `source_url` is the normal case, never counted here.
        source_urls_rejected: List[Dict[str, Any]] = []
        with self._engine.connect() as doc_conn:
            for raw_doc in documents:
                doc = {k: v for k, v in raw_doc.items() if not k.startswith("_")}
                doc_id = doc.get("doc_id")
                corpus_id = doc.get("corpus_id")
                if not doc_id or not corpus_id:
                    continue
                declared_corpus_ids.add(corpus_id)
                stable_id = doc.get("stable_id") or None
                path = doc.get("path") or None

                existing = None
                if stable_id:
                    existing_id = sources_repo.resolve(corpus_id, stable_id)
                    if existing_id:
                        existing = cf_repo.get(existing_id)
                if existing is None and path:
                    existing = cf_repo.get_by_path(corpus_id, path)

                if existing is not None:
                    file_id = existing["id"]
                    if stable_id:
                        validated_url, url_reject_reason = _validate_source_url(doc.get("source_url"))
                        if url_reject_reason:
                            source_urls_rejected.append({"doc_id": doc_id, "reason": url_reject_reason})
                        sources_repo.upsert(
                            corpus_file_id=file_id,
                            corpus_id=corpus_id,
                            source_stable_id=stable_id,
                            source_doc_id=doc_id,
                            source_sha256=doc.get("sha256") or None,
                            source_url=validated_url,
                        )
                    # A path-only match (no `stable_id`) never gets a
                    # `corpus_file_sources` row above — this direct write is
                    # the ONLY resolution for that document entry, so it
                    # must stand even when no duplicate-detection query
                    # below would ever see it.
                    doc_id_resolution[(corpus_id, doc_id)] = file_id
                    doc_declared_pairs.add((corpus_id, doc_id))
                else:
                    # Neither stable_id nor path resolved a row directly —
                    # fall back to an ALREADY-ESTABLISHED source_doc_id
                    # mapping WITHIN THIS document's own declared corpus (a
                    # prior upload/ingest resolved this doc_id once already,
                    # in this same collection) so `modified` still attaches
                    # even though THIS row carries no fresh identity to
                    # match. Scoped to corpus_id — a byte-identical copy
                    # living in ANOTHER collection must never answer for
                    # this one; that would mis-scope the claim's visibility
                    # onto the wrong collection's grants.
                    row = doc_conn.execute(
                        sa.text(
                            "SELECT corpus_file_id FROM corpus_file_sources "
                            "WHERE corpus_id = :corpus_id AND source_doc_id = :doc_id "
                            "ORDER BY corpus_file_id LIMIT 1"
                        ),
                        {"corpus_id": corpus_id, "doc_id": doc_id},
                    ).first()
                    file_id = row[0] if row is not None else None
                    if file_id is not None:
                        doc_id_resolution[(corpus_id, doc_id)] = file_id
                        doc_declared_pairs.add((corpus_id, doc_id))

                if file_id is not None:
                    parsed = _parse_document_date(doc.get("modified"))
                    if parsed is not None:
                        # P2 review finding: keyed by `doc_id`, NOT `file_id`.
                        # TCRD-241's deterministic override (below) can
                        # re-point a doc_id's resolution at a DIFFERENT
                        # corpus_file_id than the one THIS entry resolved to
                        # (an older, already-indexed copy winning over the
                        # fresh one THIS batch declared) — a file_id-keyed
                        # date would then look up a key nothing set,
                        # silently writing `document_date=NULL` and handing
                        # the succession/latest-wins projection a stale,
                        # dated claim over the new undated one.
                        doc_dates[doc_id] = parsed

        def _copies_for(corpus_id: str, doc_id: str, conn) -> List[Dict[str, Any]]:
            """Every ``corpus_files`` row anchored to ``(corpus_id, doc_id)``
            via ``corpus_file_sources`` — indexed copies first, then
            ``corpus_file_id`` as a deterministic tiebreak. More than one row
            can legally match (TCRD-241): resolution must never depend on
            scan order, and REPLACE mode needs the full set, not just the
            winner."""
            return list(
                conn.execute(
                    sa.text(
                        "SELECT cfs.corpus_file_id, cf.processing_status "
                        "FROM corpus_file_sources cfs "
                        "JOIN corpus_files cf ON cf.id = cfs.corpus_file_id "
                        "WHERE cfs.corpus_id = :corpus_id AND cfs.source_doc_id = :doc_id "
                        "ORDER BY (cf.processing_status = 'indexed') DESC, cfs.corpus_file_id"
                    ),
                    {"corpus_id": corpus_id, "doc_id": doc_id},
                )
                .mappings()
                .all()
            )

        # Where this batch's OWN documents[] entries genuinely produced MORE
        # THAN ONE `corpus_file_sources` row for the same (corpus_id,
        # doc_id) (TCRD-241 duplicate anchors), override the arbitrary
        # last-array-entry pick above with a deterministic, indexed-
        # preferred winner. A pair with zero or one row (incl. every
        # path-only match, which never writes a `corpus_file_sources` row at
        # all) keeps its direct resolution untouched.
        with self._engine.connect() as conn:
            for corpus_id, doc_id in doc_declared_pairs:
                copies = _copies_for(corpus_id, doc_id, conn)
                if len(copies) > 1:
                    doc_id_resolution[(corpus_id, doc_id)] = copies[0]["corpus_file_id"]

        # A claim's evidence carries only `doc_id` (no corpus) — map each
        # declared doc_id to exactly ONE corpus for that lookup. Normally a
        # doc_id is declared under a single corpus; on the rare case a batch
        # legitimately declares the SAME doc_id under two different corpora,
        # pick deterministically (smallest corpus_id) rather than whichever
        # happened to sort last.
        doc_id_to_corpus: Dict[str, str] = {}
        for corpus_id, doc_id in doc_declared_pairs:
            if doc_id not in doc_id_to_corpus or corpus_id < doc_id_to_corpus[doc_id]:
                doc_id_to_corpus[doc_id] = corpus_id

        # P1 review follow-up: gate tier 3 on whether `documents[]` was
        # DECLARED AT ALL (`declared_corpus_ids`, every corpus a documents[]
        # entry NAMED, whether or not that entry went on to resolve) —
        # never on `doc_declared_pairs` alone, which only holds entries that
        # actually resolved and so silently drops the scope of a rename-race
        # entry (see the ladder docstring's 3a).
        batch_corpus_ids = sorted(declared_corpus_ids)
        # RBAC review (PR #1736, TCRD-241 follow-up): a doc_id that resolves
        # ONLY by escaping every corpus THIS batch's `documents[]` declared
        # is rejected, never written — see the ladder docstring below.
        ambiguous_doc_ids: Set[str] = set()

        def _resolve_doc(doc_id: Optional[str], conn) -> Optional[str]:
            """Corpus-scoped, deterministic doc_id -> corpus_file_id
            resolution (TCRD-241). Ladder:

            1. This batch's OWN `documents[]` declared (corpus_id, doc_id)
               AND it resolved — indexed-preferred among any duplicate
               copies within it.
            2. Not declared this batch (or declared but unresolved — a
               rename race the upsert loop tolerates without erroring):
               scan only the corpora THIS batch's `documents[]` NAMED
               (`declared_corpus_ids` — every entry's own corpus_id,
               regardless of whether that entry itself resolved) — a
               producer batch is normally scoped to one collection, so an
               omitted-but-already-resolved doc_id from the SAME crawl run
               is overwhelmingly likely to live there too.
            3a. This batch's `documents[]` declared at LEAST ONE corpus
                (`batch_corpus_ids` non-empty) but this doc_id isn't
                anchored in ANY of them: refuse to escape to some OTHER,
                possibly more broadly-granted corpus — that would grant the
                claim wider visibility than the producer's batch ever
                declared (RBAC review PR #1736). This also covers a
                documents[] entry that named a corpus but never itself
                resolved (P1 follow-up) — `declared_corpus_ids` still
                carries that corpus, so the scoped scan below (not tier 3)
                runs even though `doc_declared_pairs` has no matching pair.
                A probe checks whether the doc_id resolves ANYWHERE at all,
                purely to distinguish the rejection reason
                (`ambiguous_cross_collection_doc_id` — it exists, just
                outside this batch's scope) from a doc_id that plain
                doesn't exist (`unresolved_doc_id`, existing behavior) —
                nothing is ever written on this path.
            3b. This batch's `documents[]` is EMPTY — no entry named ANY
                corpus at all, i.e. `documents` itself was omitted or every
                entry lacked a `doc_id`/`corpus_id` (no batch-declared scope
                to escape) — the documented "documents may be omitted when
                every doc_id already resolves" replay flow (spec §7.2).
                Tier 3 here is the SOLE resolution mechanism by design
                (dozens of existing callers depend on it), so it still
                resolves via an unrestricted, deterministically ordered
                global scan, unchanged from before this review.
            """
            if not doc_id:
                return None
            corpus_id = doc_id_to_corpus.get(doc_id)
            if corpus_id is not None:
                key = (corpus_id, doc_id)
                if key in doc_id_resolution:
                    return doc_id_resolution[key]

            if batch_corpus_ids:
                row = conn.execute(
                    sa.text(
                        "SELECT cfs.corpus_id, cfs.corpus_file_id "
                        "FROM corpus_file_sources cfs "
                        "JOIN corpus_files cf ON cf.id = cfs.corpus_file_id "
                        "WHERE cfs.corpus_id = ANY(:corpus_ids) AND cfs.source_doc_id = :doc_id "
                        "ORDER BY cfs.corpus_id, (cf.processing_status = 'indexed') DESC, cfs.corpus_file_id "
                        "LIMIT 1"
                    ),
                    {"corpus_ids": list(batch_corpus_ids), "doc_id": doc_id},
                ).first()
                if row is not None:
                    doc_id_resolution[(row[0], doc_id)] = row[1]
                    doc_id_to_corpus[doc_id] = row[0]
                    return row[1]
                # Not anchored in any corpus this batch declared. Probe
                # (read-only, no write) whether it resolves at all, purely
                # to pick the rejection reason — never resolve or write it.
                probe = conn.execute(
                    sa.text("SELECT 1 FROM corpus_file_sources WHERE source_doc_id = :doc_id LIMIT 1"),
                    {"doc_id": doc_id},
                ).first()
                if probe is not None:
                    ambiguous_doc_ids.add(doc_id)
                return None

            row = conn.execute(
                sa.text(
                    "SELECT cfs.corpus_id, cfs.corpus_file_id "
                    "FROM corpus_file_sources cfs "
                    "JOIN corpus_files cf ON cf.id = cfs.corpus_file_id "
                    "WHERE cfs.source_doc_id = :doc_id "
                    "ORDER BY cfs.corpus_id, (cf.processing_status = 'indexed') DESC, cfs.corpus_file_id "
                    "LIMIT 1"
                ),
                {"doc_id": doc_id},
            ).first()
            if row is None:
                return None
            doc_id_resolution[(row[0], doc_id)] = row[1]
            doc_id_to_corpus[doc_id] = row[0]
            return row[1]

        with self._engine.connect() as ro_conn:
            # Also resolve every `full_documents` id even when it carries NO
            # evidence in this batch at all — a lone "clear this document's
            # claims" replace-mode call (the deletion-driven cascade path,
            # not just a re-extraction) must still be able to find its
            # corpus_file_id.
            to_resolve = set(per_doc_claims) | set(full_documents)
            unresolved = {doc_id for doc_id in to_resolve if _resolve_doc(doc_id, ro_conn) is None}
        # §7.2: "documents may be omitted only when every referenced doc_id
        # already resolves; otherwise the batch is rejected with the
        # unresolved ids itemized." When `documents` WAS supplied but a
        # doc_id still doesn't resolve (content genuinely never uploaded),
        # that is a per-claim rejection below, not a whole-batch reject —
        # supplying the array is exactly the mechanism meant to establish
        # resolution, and it may legitimately fail for a subset.
        if not documents and unresolved:
            raise IngestUnresolvedDocIds(sorted(unresolved))

        # ---- full_documents replace mode: delete ALL existing claims for
        # EVERY corpus_file anchored to a listed doc_id WITHIN its declaring
        # corpus (not just the one resolution currently prefers) BEFORE any
        # incoming claim is written — so a subject the re-extraction no
        # longer mentions loses its stale claim (spec §7.2, test C2), and a
        # stale claim stranded on a NON-preferred duplicate copy (e.g. left
        # over from before indexed-preference picked a different winner)
        # never survives a replace either (TCRD-241).
        replaced_file_ids: Set[str] = set()
        if full_documents:
            with self._engine.connect() as conn:
                for d in full_documents:
                    corpus_id = doc_id_to_corpus.get(d)
                    if corpus_id is None:
                        continue
                    for copy in _copies_for(corpus_id, d, conn):
                        replaced_file_ids.add(copy["corpus_file_id"])
        if replaced_file_ids:
            # TCRD-296 gap #73b (live finding, 2026-09-04): this used to
            # DELETE, then call `rebuild_collection_stats` on the affected
            # corpora — "rebuilding now sets an accurate baseline that
            # add_claim's incremental hook then keeps current as this
            # batch's own writes land" (TCRD-296 E.21). That baseline is
            # itself the expensive, racy part on a hot path: replace mode
            # runs once per ~25-document batch, so four concurrent
            # facts-extraction passes over one 2.4M-claim collection turned
            # EVERY batch into a full per-collection DELETE + plain
            # `INSERT ... SELECT ... GROUP BY` racing every other pass's
            # incremental `_bump_collection_stats_impl` (`INSERT ... ON
            # CONFLICT DO UPDATE`) — 30+ unique-constraint violations and a
            # detected deadlock in 15 minutes, each one a document whose
            # claims had ALREADY been deleted here and were then never
            # reconciled (net claim loss). `_decrement_collection_stats_
            # for_deleted_claims` is the exact inverse of the bump, bounded
            # by THIS delete's own returned rows — the same mechanism
            # `delete_claims_for_file` already uses for the identical
            # reason, run inside the SAME transaction as the delete it
            # reconciles so the two can never drift apart.
            with self._engine.begin() as conn:
                deleted_rows = (
                    conn.execute(
                        sa.text(
                            "DELETE FROM claims WHERE corpus_file_id = ANY(:ids) RETURNING corpus_id, fact_id, edge_id"
                        ),
                        {"ids": list(replaced_file_ids)},
                    )
                    .mappings()
                    .all()
                )
                self._decrement_collection_stats_for_deleted_claims(conn, deleted_rows)

        file_row_cache: Dict[str, Optional[dict]] = {}

        def _file_row(file_id: str) -> Optional[dict]:
            if file_id not in file_row_cache:
                file_row_cache[file_id] = cf_repo.get(file_id)
            return file_row_cache[file_id]

        chunk_cache: Dict[str, List[str]] = {}

        def _chunk_texts(file_id: str) -> List[str]:
            if file_id not in chunk_cache:
                chunk_cache[file_id] = [c["text"] for c in chunks_repo.list_for_file(file_id) if c.get("text")]
            return chunk_cache[file_id]

        joined_cache: Dict[str, str] = {}

        def _joined_text(file_id: str) -> str:
            """The document rebuilt from its chunks, ONCE per file per batch.

            The boundary-crossing check below joins the whole document, and it
            runs per QUOTE. A batch where many quotes miss their individual
            chunks — exactly the batch this repair path exists for — rebuilt
            and rescanned the entire document once for each of them, so the
            cost grew with quotes x document size (Devin Review on #2063).
            Lazy on purpose: a batch whose quotes all land inside a single
            chunk never joins anything, which is the common case the per-chunk
            check above is ordered first to serve.
            """
            if file_id not in joined_cache:
                joined_cache[file_id] = CHUNK_JOIN_SEPARATOR.join(_chunk_texts(file_id))
            return joined_cache[file_id]

        claims_written = 0
        # Per-doc_id breakdown of `claims_written` (TCRD-296 gap #62) — a
        # producer folding this batch's report into a per-document ledger
        # cannot otherwise tell "my document's own evidence was accepted"
        # from "the batch overall wrote claims", which is what let a
        # ledger entry read `done` for a document that in fact contributed
        # zero claims (an ingest-time rejection, or — see
        # `resolved_file_by_doc` below — a TCRD-241 duplicate copy whose
        # claims all landed on a SIBLING file).
        claims_written_by_doc: Dict[str, int] = {}
        # doc_id -> the `corpus_file_id` its evidence actually resolved to
        # THIS call (set the moment `_resolve_doc` succeeds, regardless of
        # whether that particular evidence item went on to be written or
        # rejected). Normally that is simply "this document's own file",
        # but TCRD-241 collapses every duplicate copy of one (corpus_id,
        # doc_id) onto ONE deterministic winner `corpus_file_id` — so a
        # caller keying its own ledger on doc_id can compare this against
        # the file_id IT declared for that doc_id and tell "my claims are
        # here" from "my claims are on my duplicate sibling instead".
        resolved_file_by_doc: Dict[str, str] = {}
        claims_accepted_via_identity = 0
        claims_rejected: List[Dict[str, Any]] = []
        deferred: List[Dict[str, Any]] = []
        subjects_created = 0
        review_items: List[Dict[str, Any]] = []
        corrections_active: List[Dict[str, Any]] = []
        touched_fact_ids: set = set()
        touched_edge_pairs: set = set()
        # Edges skipped because `create_edge` hit `EdgeEndpointMissing` — an
        # endpoint fact resolved fine but was gone by the time the edge
        # INSERT ran (see that exception's docstring). Counted, never
        # raised: the race is inherent to concurrent passes sharing one
        # fact graph, not a caller bug.
        edges_skipped_missing_endpoint = 0
        single_valued_types = _single_valued_edge_types()
        # (node_id, fact_id, type) for every fact THIS batch newly minted —
        # fed to `_propose_duplicate_candidates` once the node loop below
        # is done resolving. `duplicate_pairs_proposed` is the unordered-
        # pair dedupe set that spans both directions within this one call.
        newly_created_facts: List[Tuple[str, str, str]] = []
        duplicate_pairs_proposed: set = set()

        def _write_evidence(
            *,
            kind: str,
            subject_id: str,
            evidence: List[Dict[str, Any]],
            row_ref: str,
            conn: Connection,
            row_attrs: Optional[Dict[str, Any]] = None,
            alias_targets: Optional[List[Tuple[str, str]]] = None,
        ) -> None:
            """``alias_targets`` — ``[(type, natural_key), ...]`` — names
            the alias(es) THIS evidence is establishing/reinforcing:
            security hardening (module docstring's alias-visibility rule)
            records each evidence item's corpus as provenance for exactly
            those aliases, never for every alias a fact happens to carry —
            the distinction that closes the bug (a fact's OTHER, unrelated
            readable claim must never grant visibility to a name minted
            from a different corpus).

            A node's own evidence (``kind="fact"``) targets its OWN single
            alias. An edge's evidence (``kind="edge"``) targets BOTH
            endpoint aliases — an edge's claim evidences its endpoints too
            (module docstring, "Endpoint evidence"), so a node that exists
            ONLY as an edge anchor (zero claims of its own — the common
            `works_in_industry`/`sponsored_by`/`staffed_by`-style ontology
            shape) still gets its alias's provenance from the edge that
            names it, never staying permanently admin-only. The edge
            itself carries no alias of its own (edges have none).

            ``conn`` is the CALLER's already-open transaction (the node's or
            edge's own — see the node/edge loops below), not one this
            function opens itself: the subject's evidence must commit or
            roll back atomically WITH the fact/edge row it evidences, so a
            fact is never visible to a concurrent call's `sweep_orphans()`
            with zero claims when it in fact has some pending in this same
            batch (`EdgeEndpointMissing`'s docstring)."""
            nonlocal claims_written, claims_accepted_via_identity
            for ev_idx, ev in enumerate(evidence):
                doc_id = ev.get("doc_id")
                quote = ev.get("quote") or ""
                item_ref = f"{row_ref}.evidence[{ev_idx}]"
                if not quote:
                    claims_rejected.append({"row": item_ref, "reason": "empty_quote", "doc_id": doc_id})
                    continue
                if not _is_meaningful_quote(quote):
                    # Applied BEFORE either half of the gate below, so a
                    # degenerate quote (a bare file-extension fragment, a
                    # lone separator) cannot fall through the content
                    # check and be self-certified by the identity
                    # haystack instead — one check closes the hole on
                    # both paths. Distinct reason from
                    # `verbatim_gate_failed`: the quote WAS present
                    # verbatim (or would be, trivially), it just isn't
                    # evidence of anything — a different failure an
                    # operator should be able to tell apart (a producer
                    # citing junk vs. a producer citing text absent from
                    # the document).
                    claims_rejected.append({"row": item_ref, "reason": "quote_not_meaningful", "doc_id": doc_id})
                    continue
                file_id = _resolve_doc(doc_id, conn)
                frow = _file_row(file_id) if file_id else None
                if file_id is None or frow is None:
                    reason = "ambiguous_cross_collection_doc_id" if doc_id in ambiguous_doc_ids else "unresolved_doc_id"
                    claims_rejected.append({"row": item_ref, "reason": reason, "doc_id": doc_id})
                    continue
                # Recorded as soon as doc_id resolves at all — independent
                # of whether THIS evidence item goes on to be written or
                # rejected (e.g. `verbatim_gate_failed` below): the mapping
                # answers "where does doc_id live", not "did this specific
                # citation succeed".
                resolved_file_by_doc[doc_id] = file_id
                if frow.get("processing_status") != "indexed":
                    deferred.append(
                        {
                            "row": item_ref,
                            "doc_id": doc_id,
                            "corpus_file_id": file_id,
                            "reason": "not_indexed",
                            "retry_after_seconds": 60,
                        }
                    )
                    continue
                texts = _chunk_texts(file_id)
                accepted_via_identity = False
                # Per-chunk first (the common case, and the cheaper
                # check); only join the whole document when no single
                # chunk contains it, so a boundary-crossing quote still
                # gets a fair look before falling through to identity
                # (cost-levers spec §2.1(b)/§2.2 — see
                # `CHUNK_JOIN_SEPARATOR`'s docstring above).
                if not any(quote in t for t in texts) and quote not in _joined_text(file_id):
                    # Widened gate: the document's own SERVER-STORED
                    # identity (`corpus_files.filename`/`path`) counts as
                    # verbatim evidence too — the extraction ontology
                    # legitimately grounds a claim in a document's folder
                    # path + filename (e.g. a `part_of` edge citing
                    # "Project Kemp/Parts Authority — …pptx"), and those
                    # quotes have no chunk to land in (spec §8).
                    # Deliberately `frow` (fetched from `corpus_files`
                    # above), NEVER anything off the wire (`doc`/`ev`) —
                    # a producer-declared name/path is used only to
                    # RESOLVE which row this evidence is about, never as
                    # evidence itself, or a producer could self-certify
                    # an invented quote by declaring whatever string it
                    # likes. P0 review finding: the quote must EQUAL a
                    # whole identity unit (`_identity_candidates`) —
                    # never merely a substring of one, which admitted a
                    # bare ".pptx" or "/" and let a fabricated attribute
                    # ride in as a confidently-cited quote. No
                    # normalization is applied here, matching the
                    # chunk-text check above exactly — an NFC/NFD form
                    # mismatch fails identically on both sides.
                    if quote in _identity_candidates(frow.get("filename"), frow.get("path")):
                        accepted_via_identity = True
                    else:
                        claims_rejected.append({"row": item_ref, "reason": "verbatim_gate_failed", "doc_id": doc_id})
                        continue
                written_id = self.add_claim(
                    fact_id=subject_id if kind == "fact" else None,
                    edge_id=subject_id if kind == "edge" else None,
                    corpus_file_id=file_id,
                    corpus_id=frow["corpus_id"],
                    file_sha256=frow.get("sha256") or "",
                    quote=quote,
                    # Per-evidence `attrs` isn't part of the producer's
                    # wire format (spec §7.0) — `attrs` sits on the
                    # node/edge row itself ("what THIS document says",
                    # §3), and the concatenated-per-document pipeline
                    # output means one row object == one document's
                    # occurrence, so the row's own attrs is what each
                    # of its claims should carry. An evidence-level
                    # `attrs` is honored first if a future producer
                    # ever supplies one (forward-compatible, unused
                    # today).
                    attrs=ev.get("attrs") or row_attrs or {},
                    # P2 review finding: looked up by the EVIDENCE'S OWN
                    # `doc_id`, not the resolved `file_id` — the
                    # TCRD-241 deterministic override can resolve this
                    # doc_id to a corpus_file_id THIS batch never itself
                    # declared a date for (see `doc_dates`' definition
                    # above), which previously wrote `document_date` as
                    # silently NULL.
                    document_date=doc_dates.get(doc_id),
                    # Index-time audience-variant tag (Task 10, spec
                    # §4.2) — format-validated up front by
                    # `app/api/facts.py` before `ingest_batch` ever
                    # runs, so a malformed value here would already
                    # have been refused with a 422; this stores
                    # whatever survived that gate, verbatim.
                    audience=ev.get("audience"),
                    conn=conn,
                )
                for alias_type, alias_natural_key in alias_targets or []:
                    # Recorded regardless of `written_id` (a replayed,
                    # already-existing claim still means this corpus
                    # genuinely evidences the alias — the provenance
                    # set only grows, see `add_alias_source`). A no-op
                    # if the alias row hasn't been minted yet (it
                    # always has by this point — `_resolve_alias` runs
                    # before any evidence write, for both nodes and
                    # edge endpoints).
                    self.add_alias_source(
                        type=alias_type, natural_key=alias_natural_key, corpus_id=frow["corpus_id"], conn=conn
                    )
                if written_id is not None:
                    claims_written += 1
                    claims_written_by_doc[doc_id] = claims_written_by_doc.get(doc_id, 0) + 1
                    if accepted_via_identity:
                        claims_accepted_via_identity += 1
                    if kind == "fact":
                        touched_fact_ids.add(subject_id)

        # ---- nodes: alias resolution + evidence.
        node_fact_ids: Dict[str, str] = {}
        # Parallel to `node_fact_ids` — the alias's own `type` (spec §3:
        # denormalized onto `fact_aliases`, not always the node id's own
        # `<type>:` prefix, since a producer-declared `type` can override
        # it in `_resolve_alias`). `_endpoint()`'s already-resolved-this-
        # batch fast path below needs it for `alias_targets`, same as the
        # freshly-resolved path already gets from `_resolve_alias`.
        node_types: Dict[str, str] = {}
        for idx, node in enumerate(nodes):
            node_id = node.get("id")
            row_ref = f"nodes[{idx}]"
            if not node_id:
                claims_rejected.append({"row": row_ref, "reason": "missing_node_id"})
                continue
            # ATOMICITY: alias resolution (which may mint a new fact + its
            # alias), correction re-attachment, and this node's OWN evidence
            # all share ONE transaction — a partial write (e.g. a claim
            # insert failing mid-loop) rolls the fact back too, rather than
            # leaving a fact with none of its own evidence for a concurrent
            # `sweep_orphans()` to race against. See `EdgeEndpointMissing`'s
            # docstring for the race this closes.
            with self._engine.begin() as conn:
                resolution = self._resolve_alias(conn, node_id, node.get("type"))
                if resolution["error"] is not None:
                    claims_rejected.append({"row": row_ref, "reason": resolution["error"], "node_id": node_id})
                    continue
                if resolution["created"]:
                    subjects_created += 1
                    if resolution.get("reattached"):
                        corrections_active.append(resolution["reattached"])
                    newly_created_facts.append((node_id, resolution["fact_id"], resolution["type"]))
                node_fact_ids[node_id] = resolution["fact_id"]
                node_types[node_id] = resolution["type"]
                _write_evidence(
                    kind="fact",
                    subject_id=resolution["fact_id"],
                    evidence=node.get("evidence") or [],
                    row_ref=row_ref,
                    conn=conn,
                    row_attrs=node.get("attrs") or {},
                    alias_targets=[(resolution["type"], node_id)],
                )

        # ---- edges: endpoints resolve via the SAME alias mechanism (an
        # edge's src/dst are themselves node ids, spec §7.0) + evidence.
        # `possible_duplicate_of` is the one edge type exempt from
        # requiring evidence (§7.0) -- accepted and surfaced as a review
        # item (§7.2) regardless of whether it carries any.
        def _endpoint(node_id: str, conn) -> Dict[str, Any]:
            if node_id in node_fact_ids:
                return {
                    "fact_id": node_fact_ids[node_id],
                    "type": node_types[node_id],
                    "created": False,
                    "error": None,
                    "reattached": None,
                }
            return self._resolve_alias(conn, node_id, None)

        for idx, edge in enumerate(edges):
            row_ref = f"edges[{idx}]"
            src_id, edge_type, dst_id = edge.get("src"), edge.get("type"), edge.get("dst")
            if not src_id or not edge_type or not dst_id:
                claims_rejected.append({"row": row_ref, "reason": "malformed_edge"})
                continue
            # `.begin()`, not `.connect()`: `_endpoint()` may mint a brand
            # new fact via `_resolve_alias` -> `create_fact(conn=conn)` — a
            # real write that must commit regardless of whether THIS edge
            # itself later succeeds, exactly like the pre-atomicity code's
            # own `create_fact()` always did on its own internal
            # transaction. A plain `.connect()` here would silently roll
            # that INSERT back on `with` exit (SQLAlchemy 2.0's implicit-
            # transaction-rolls-back-if-uncommitted default), leaving
            # `create_edge` below referencing a `dst`/`src` that was never
            # actually persisted — a self-inflicted, always-reproducible
            # version of the same FK violation `EdgeEndpointMissing` exists
            # to catch for the genuine cross-process race.
            with self._engine.begin() as conn:
                src_res = _endpoint(src_id, conn)
                dst_res = _endpoint(dst_id, conn)
            if src_res.get("error") or dst_res.get("error"):
                claims_rejected.append({"row": row_ref, "reason": "edge_endpoint_unresolved"})
                continue
            for endpoint_id, res in ((src_id, src_res), (dst_id, dst_res)):
                if res.get("created"):
                    subjects_created += 1
                    # Keep `node_fact_ids`/`node_types` parallel (see the
                    # comment where they're declared, above) — omitting
                    # `node_types` here left `_endpoint()`'s "already
                    # resolved this batch" fast path indexing a key that
                    # was never written, raising `KeyError` the moment a
                    # SECOND edge in the same batch referenced this same
                    # resolved-not-emitted endpoint (issue: an edge
                    # endpoint minted here, rather than via the nodes[]
                    # loop above, never got a `node_types` entry at all).
                    node_fact_ids[endpoint_id] = res["fact_id"]
                    node_types[endpoint_id] = res["type"]
                    if res.get("reattached"):
                        corrections_active.append(res["reattached"])
                    newly_created_facts.append((endpoint_id, res["fact_id"], res["type"]))

            # ATOMICITY: the edge row, its correction re-attachment, and its
            # OWN evidence all share ONE transaction — same reasoning as the
            # node loop above. `create_edge` raises `EdgeEndpointMissing`
            # when Postgres's FK constraint rejects the INSERT because `src`/
            # `dst` no longer exists (resolved moments ago above, but
            # deleted since by a concurrent pass's merge/dedup or its own
            # `sweep_orphans()`) — that is a genuine, expected race, not a
            # caller bug, so it is counted and this ONE edge is skipped
            # rather than failing the whole batch.
            try:
                with self._engine.begin() as conn:
                    edge_id = self.create_edge(
                        src=src_res["fact_id"], type=edge_type, dst=dst_res["fact_id"], conn=conn
                    )
                    reattached_edge = self._reattach_correction(
                        conn, subject_kind="edge", subject_id=edge_id, natural_key_probe=[src_id, edge_type, dst_id]
                    )
                    if reattached_edge:
                        corrections_active.append(reattached_edge)

                    if edge_type == "possible_duplicate_of":
                        review_items.append(
                            {"type": "possible_duplicate_of", "edge_id": edge_id, "src": src_id, "dst": dst_id}
                        )
                    if edge_type in single_valued_types:
                        touched_edge_pairs.add((src_res["fact_id"], edge_type))
                    _write_evidence(
                        kind="edge",
                        subject_id=edge_id,
                        evidence=edge.get("evidence") or [],
                        row_ref=row_ref,
                        conn=conn,
                        row_attrs=edge.get("attrs") or {},
                        # An edge's claim evidences BOTH endpoints too (module
                        # docstring, "Endpoint evidence") — a node with zero
                        # claims of its own, reachable only as an edge anchor
                        # (the common works_in_industry/sponsored_by/staffed_by
                        # ontology shape), must still get its alias's provenance
                        # from here, or it stays permanently admin-only despite
                        # being visible and findable by existence.
                        alias_targets=[(src_res["type"], src_id), (dst_res["type"], dst_id)],
                    )
            except EdgeEndpointMissing:
                edges_skipped_missing_endpoint += 1
                logger.warning(
                    "facts ingest: skipping edge %s -[%s]-> %s (%s) — endpoint fact missing, "
                    "likely merged/deduplicated by a concurrent pass",
                    src_id,
                    edge_type,
                    dst_id,
                    row_ref,
                )
                continue

        # ---- entity-resolution candidates (spec's own `entity_resolution`
        # convention: extraction never guess-merges, a probable-but-unsure
        # match earns a `possible_duplicate_of` edge for human review) --
        # every fact THIS batch newly minted, compared against every OTHER
        # same-type alias already in the graph. Runs after BOTH the node
        # and edge loops so it also covers a fact minted purely as an edge
        # endpoint (`_endpoint()`'s fallback above), not only ones listed
        # in `nodes[]`.
        if newly_created_facts:
            with self._engine.connect() as conn:
                review_items.extend(
                    self._propose_duplicate_candidates(
                        conn, new_facts=newly_created_facts, already_proposed=duplicate_pairs_proposed
                    )
                )

        # ---- functionally single-valued edges (spec §7.3): a (src, type)
        # pair TOUCHED by this batch (an edge of a configured type was just
        # written for it) with >1 distinct dst carrying a LIVE claim becomes
        # a review item. The query hits `edges`/`claims` directly rather
        # than this batch's rows, so a dst written by an EARLIER batch is
        # picked up the moment a second one arrives now (never persisted —
        # recomputed here, and again at read time in
        # `collection_facts_summary`, so it clears the instant a dst's
        # claims are gone).
        if touched_edge_pairs:
            srcs = list({pair[0] for pair in touched_edge_pairs})
            types_ = list({pair[1] for pair in touched_edge_pairs})
            with self._engine.connect() as conn:
                sv_rows = (
                    conn.execute(
                        sa.text(
                            """
                            SELECT e.src, e.type, e.dst
                            FROM edges e
                            WHERE e.src = ANY(:srcs) AND e.type = ANY(:types)
                              AND EXISTS (SELECT 1 FROM claims c WHERE c.edge_id = e.id)
                            """
                        ),
                        {"srcs": srcs, "types": types_},
                    )
                    .mappings()
                    .all()
                )
            dsts_by_pair: Dict[tuple, set] = {}
            for r in sv_rows:
                pair = (r["src"], r["type"])
                if pair in touched_edge_pairs:
                    dsts_by_pair.setdefault(pair, set()).add(r["dst"])
            for (src_fact_id, edge_type_), dsts in dsts_by_pair.items():
                if len(dsts) > 1:
                    review_items.append(
                        {
                            "kind": "single_valued_conflict",
                            "src": src_fact_id,
                            "type": edge_type_,
                            "dsts": sorted(dsts),
                        }
                    )

        # ---- same-date attribute conflicts introduced by THIS batch become
        # review items (EQ4) -- different-date succession needs none (EQ5),
        # already correct at READ time via search()'s projection; this is
        # the write-side signal a human reviewer would want surfaced now.
        if touched_fact_ids:
            with self._engine.connect() as conn:
                conflict_rows = (
                    conn.execute(
                        sa.text(
                            """
                            SELECT c.fact_id, kv.key, c.document_date, jsonb_agg(DISTINCT kv.value) AS values
                            FROM claims c
                            CROSS JOIN LATERAL jsonb_each(c.attrs) AS kv(key, value)
                            WHERE c.fact_id = ANY(:ids) AND c.document_date IS NOT NULL
                            GROUP BY c.fact_id, kv.key, c.document_date
                            HAVING COUNT(DISTINCT kv.value) > 1
                            """
                        ),
                        {"ids": list(touched_fact_ids)},
                    )
                    .mappings()
                    .all()
                )
            for r in conflict_rows:
                review_items.append(
                    {
                        "type": "attribute_conflict",
                        "subject_id": r["fact_id"],
                        "key": r["key"],
                        "document_date": r["document_date"].isoformat(),
                        "values": _decode_jsonb(r["values"]),
                    }
                )

        # C.12 (see `run_orphan_sweep`'s docstring above): a multi-batch
        # caller opts OUT of this per-batch sweep and runs its own single
        # end-of-pass sweep instead. `sweep_skipped: False` here is honest
        # either way — it never claims a lock-contention skip that didn't
        # happen, and the real (deferred) count is never double-reported.
        if run_orphan_sweep:
            sweep_result = self.sweep_orphans(grace_seconds=orphan_sweep_grace_seconds)
        else:
            sweep_result = {"deleted": 0, "skipped": False}
        subjects_deleted = sweep_result["deleted"]

        # TCRD-241 / RBAC review (PR #1736): a doc_id that would only have
        # resolved by escaping every corpus this batch's `documents[]`
        # declared is REJECTED, not written (see `_resolve_doc` tier 3a) —
        # itemized in `claims_rejected` with reason
        # `ambiguous_cross_collection_doc_id`, same shape as every other
        # rejection reason. No separate top-level counter: `claims_rejected`
        # is already the itemized source of truth (mirrors
        # `facts_ingest_runs.claims_rejected_count`, itself `len(claims_rejected)`).

        return {
            "claims_written": claims_written,
            # TCRD-296 gap #62: per-doc_id breakdown of `claims_written`,
            # plus which `corpus_file_id` each cited doc_id actually
            # resolved to (see the two dicts' own definitions above) — the
            # pair a caller needs to correct a per-document ledger entry
            # written OPTIMISTICALLY before this call ran (a document whose
            # own evidence contributed zero claims must not stay marked
            # done; a TCRD-241 duplicate copy whose claims landed on a
            # sibling file must record that fact, not read as "missing").
            "claims_written_by_doc": claims_written_by_doc,
            "resolved_file_by_doc": resolved_file_by_doc,
            # Of `claims_written`, the subset that only passed the gate via
            # the document's SERVER-STORED filename/path — never a chunk —
            # so an operator can see how much evidence is filename-grounded
            # (weaker evidence still: §8 notes the gate validates the quote,
            # not the fact, and an identity-grounded quote grounds even
            # less).
            "claims_accepted_via_identity": claims_accepted_via_identity,
            "claims_rejected": claims_rejected,
            "source_urls_rejected": source_urls_rejected,
            "deferred": deferred,
            "subjects_created": subjects_created,
            "subjects_deleted": subjects_deleted,
            # True when this batch's own end-of-ingest sweep backed off
            # because a CONCURRENT pass already held `sweep_orphans()`'s
            # serializing advisory lock (see its docstring's "Concurrency"
            # section) — never an error, just visibility: the next pass's
            # sweep covers whatever this one skipped, so a source card that
            # reads a run of `sweep_skipped: true` alongside
            # `subjects_deleted: 0` knows nothing was left orphaned, the
            # sweep simply deferred to a sibling pass.
            "sweep_skipped": sweep_result["skipped"],
            "corrections_active": corrections_active,
            "review_items": review_items,
            # Edges NOT written because their src/dst fact was gone by the
            # time the INSERT ran — see `EdgeEndpointMissing`. Never itemized
            # per-edge (unlike `claims_rejected`): the endpoint node ids are
            # already logged, and this is a count of a race, not a producer
            # mistake to fix.
            "edges_skipped_missing_endpoint": edges_skipped_missing_endpoint,
        }

    # ------------------------------------------------------------------
    # entity-resolution repair (spec §3: "a merge is adding an alias...
    # repoint aliases + claims to the canonical subject, audit-logged; a
    # split is the reverse") — EQ7's foundation. Not on the ingest write
    # path itself (ingest never auto-merges); an admin/reconciliation
    # caller invokes these directly.
    # ------------------------------------------------------------------

    def merge_facts(self, *, canonical_id: str, merged_id: str, merged_by: str) -> Dict[str, Any]:
        """Merge ``merged_id`` INTO ``canonical_id``: repoint every alias
        and claim, delete the now-empty ``merged_id`` row, audit-log the
        action. Both facts must share a ``type`` (entity-resolution repair,
        not a type change). Returns a snapshot — ``{canonical_id,
        merged_id, aliases, claim_ids, duplicate_claims}`` — sufficient for
        :meth:`split_fact` to reverse it (union of claims, both aliases, per
        spec EQ7).

        **Shared evidence.** ``uq_claims_subject_file_quote`` is unique on
        ``(COALESCE(fact_id, edge_id), corpus_file_id, quote_hash)``, so a
        bare repoint explodes with an IntegrityError whenever the two facts
        each carry a claim from the same document with the same quote —
        which is exactly the ordinary entity-resolution case (one sentence
        evidencing "Acme Corp" and "Acme Corporation"). Those merged-side
        claims are therefore captured into ``duplicate_claims`` and deleted
        before the repoint; the canonical's identical claim survives, so no
        evidence is lost. ``split_fact`` re-creates them on the new fact from
        that snapshot, which keeps the merge reversible — under fresh claim
        ids, the same way ``split_fact`` already mints a new fact id rather
        than resurrecting ``merged_id`` (ids are opaque and never reused,
        spec §3)."""
        with self._engine.begin() as conn:
            canonical = (
                conn.execute(sa.text("SELECT type FROM facts WHERE id = :id"), {"id": canonical_id}).mappings().first()
            )
            merged = (
                conn.execute(sa.text("SELECT type FROM facts WHERE id = :id"), {"id": merged_id}).mappings().first()
            )
            if canonical is None:
                raise FactNotFound(canonical_id)
            if merged is None:
                raise FactNotFound(merged_id)
            if canonical["type"] != merged["type"]:
                raise ValueError(
                    f"merge_facts requires matching types, got {canonical['type']!r} vs {merged['type']!r}"
                )
            aliases = (
                conn.execute(sa.text("SELECT natural_key FROM fact_aliases WHERE fact_id = :id"), {"id": merged_id})
                .scalars()
                .all()
            )
            # Merged-side claims the canonical already holds for the SAME
            # (document, quote): repointing them would violate
            # uq_claims_subject_file_quote. Capture their content so the
            # split can re-create them, then drop them — the canonical's
            # identical claim carries the evidence forward.
            dup_rows = (
                conn.execute(
                    sa.text(
                        "SELECT id, corpus_file_id, corpus_id, file_sha256, attrs, quote, quote_hash, document_date "
                        "FROM claims m WHERE m.fact_id = :merged AND EXISTS ("
                        "  SELECT 1 FROM claims c WHERE c.fact_id = :canonical "
                        "    AND c.corpus_file_id = m.corpus_file_id AND c.quote_hash = m.quote_hash)"
                    ),
                    {"merged": merged_id, "canonical": canonical_id},
                )
                .mappings()
                .all()
            )
            duplicate_claims = [
                {
                    "corpus_file_id": r["corpus_file_id"],
                    "corpus_id": r["corpus_id"],
                    "file_sha256": r["file_sha256"],
                    "attrs": _decode_jsonb(r["attrs"]) or {},
                    "quote": r["quote"],
                    "quote_hash": r["quote_hash"],
                    "document_date": r["document_date"].isoformat() if r["document_date"] else None,
                }
                for r in dup_rows
            ]
            dup_ids = [r["id"] for r in dup_rows]
            if dup_ids:
                conn.execute(sa.text("DELETE FROM claims WHERE id = ANY(:ids)"), {"ids": dup_ids})

            claim_ids = (
                conn.execute(sa.text("SELECT id FROM claims WHERE fact_id = :id"), {"id": merged_id}).scalars().all()
            )
            # TCRD-296 E.21: every corpus a moved-or-dropped claim touches —
            # captured BEFORE the repoint/delete below — needs a stats
            # recompute (both `merged_id`'s membership rows, cascade-deleted
            # with the fact itself, and `canonical_id`'s, which just gained
            # these claims).
            affected_corpus_ids = set(
                conn.execute(sa.text("SELECT DISTINCT corpus_id FROM claims WHERE fact_id = :id"), {"id": merged_id})
                .scalars()
                .all()
            ) | {r["corpus_id"] for r in dup_rows}
            conn.execute(
                sa.text("UPDATE fact_aliases SET fact_id = :canonical WHERE fact_id = :merged"),
                {"canonical": canonical_id, "merged": merged_id},
            )
            conn.execute(
                sa.text("UPDATE claims SET fact_id = :canonical WHERE fact_id = :merged"),
                {"canonical": canonical_id, "merged": merged_id},
            )
            conn.execute(sa.text("DELETE FROM facts WHERE id = :id"), {"id": merged_id})

        if affected_corpus_ids:
            self.rebuild_collection_stats(corpus_ids=list(affected_corpus_ids))

        from src.repositories import audit_repo

        snapshot = {
            "canonical_id": canonical_id,
            "merged_id": merged_id,
            "aliases": list(aliases),
            "claim_ids": list(claim_ids),
            "duplicate_claims": duplicate_claims,
        }
        audit_repo().log(
            user_id=merged_by,
            action="facts.merge",
            resource=f"fact/{canonical_id}",
            params={
                "merged_id": merged_id,
                "aliases": snapshot["aliases"],
                "claim_ids": snapshot["claim_ids"],
                "duplicate_claims_dropped": len(duplicate_claims),
            },
        )
        return snapshot

    def split_fact(self, *, canonical_id: str, snapshot: Dict[str, Any], split_by: str) -> str:
        """Reverse a prior :meth:`merge_facts` using its returned
        ``snapshot``: mint a NEW fact of the canonical's type, repoint the
        snapshotted aliases + claims back onto it, and RE-CREATE any claims
        the merge had to drop as duplicates of the canonical's own evidence
        (``duplicate_claims`` — see :meth:`merge_facts`). Returns the new
        fact id — deliberately NOT the original ``merged_id``, since ids are
        opaque and never reused (spec §3); the re-created claims get fresh
        ids for the same reason."""
        aliases = snapshot.get("aliases") or []
        claim_ids = snapshot.get("claim_ids") or []
        duplicate_claims = snapshot.get("duplicate_claims") or []
        with self._engine.begin() as conn:
            row = (
                conn.execute(sa.text("SELECT type FROM facts WHERE id = :id"), {"id": canonical_id}).mappings().first()
            )
            if row is None:
                raise FactNotFound(canonical_id)
            new_id = "f_" + secrets.token_hex(8)
            conn.execute(
                sa.text("INSERT INTO facts (id, type) VALUES (:id, :type)"), {"id": new_id, "type": row["type"]}
            )
            if aliases:
                conn.execute(
                    sa.text(
                        "UPDATE fact_aliases SET fact_id = :new "
                        "WHERE natural_key = ANY(:aliases) AND fact_id = :canonical"
                    ),
                    {"new": new_id, "aliases": list(aliases), "canonical": canonical_id},
                )
            # TCRD-296 E.21: same reasoning as `merge_facts`'s hook — every
            # corpus a moved-back or re-created claim touches needs a stats
            # recompute (`canonical_id` loses these claims, `new_id` gains
            # them).
            affected_corpus_ids: Set[str] = {dup["corpus_id"] for dup in duplicate_claims}
            if claim_ids:
                moved_corpus_ids = (
                    conn.execute(
                        sa.text("SELECT DISTINCT corpus_id FROM claims WHERE id = ANY(:ids)"), {"ids": list(claim_ids)}
                    )
                    .scalars()
                    .all()
                )
                affected_corpus_ids.update(moved_corpus_ids)
                conn.execute(
                    sa.text("UPDATE claims SET fact_id = :new WHERE id = ANY(:ids)"),
                    {"new": new_id, "ids": list(claim_ids)},
                )
            for dup in duplicate_claims:
                conn.execute(
                    sa.text(
                        "INSERT INTO claims "
                        "(id, fact_id, edge_id, corpus_file_id, corpus_id, file_sha256, attrs, quote, "
                        " quote_hash, document_date) "
                        "VALUES (:id, :fact_id, NULL, :corpus_file_id, :corpus_id, :file_sha256, "
                        "        CAST(:attrs AS JSONB), :quote, :quote_hash, :document_date) "
                        "ON CONFLICT (COALESCE(fact_id, edge_id), corpus_file_id, quote_hash) DO NOTHING"
                    ),
                    {
                        "id": "c_" + secrets.token_hex(8),
                        "fact_id": new_id,
                        "corpus_file_id": dup["corpus_file_id"],
                        "corpus_id": dup["corpus_id"],
                        "file_sha256": dup.get("file_sha256") or "",
                        "attrs": json.dumps(dup.get("attrs") or {}),
                        "quote": dup["quote"],
                        "quote_hash": dup["quote_hash"],
                        "document_date": _parse_document_date(dup.get("document_date")),
                    },
                )

        if affected_corpus_ids:
            self.rebuild_collection_stats(corpus_ids=list(affected_corpus_ids))

        from src.repositories import audit_repo

        audit_repo().log(
            user_id=split_by,
            action="facts.split",
            resource=f"fact/{canonical_id}",
            params={
                "new_id": new_id,
                "aliases": aliases,
                "claim_ids": claim_ids,
                "duplicate_claims_recreated": len(duplicate_claims),
            },
        )
        return new_id


# O7 (spec §8/§8.1): a citation deep link the caller renders as a clickable
# href — never dialed by Agnes itself (the canonical-source contract). This
# is display-safety validation, not a reachability check: https-only blocks
# `javascript:`/`data:`/plain `http:` outright, and a length cap bounds an
# otherwise-unbounded producer string before it reaches storage.
_MAX_SOURCE_URL_LEN = 2048


def _validate_source_url(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort validation of a producer-supplied ``source_url`` (spec
    §8, O7). Returns ``(validated_url, rejection_reason)`` — same
    tolerant-input contract as :func:`_parse_document_date`: an invalid
    value is dropped from storage (the first element is ``None``) and the
    surrounding document/claims still ingest, never raises.

    The second element distinguishes "nothing sent" from "something sent
    but rejected" — both leave ``validated_url`` ``None``, but only the
    latter is worth surfacing on the ingest run report
    (``source_urls_rejected``, O7 follow-up): a producer that never sends
    ``source_url`` is not an anomaly, a producer whose values Agnes keeps
    refusing is. ``reason`` is ``None`` whenever ``value`` is absent/blank
    OR the url validated successfully; otherwise one of ``too_long`` /
    ``unparseable`` / ``not_https`` / ``no_host``.
    """
    if not value:
        return None, None
    s = str(value).strip()
    if not s:
        return None, None
    if len(s) > _MAX_SOURCE_URL_LEN:
        return None, "too_long"
    try:
        parsed = urlsplit(s)
    except ValueError:
        return None, "unparseable"
    if parsed.scheme.lower() != "https":
        return None, "not_https"
    if not parsed.netloc:
        return None, "no_host"
    return s, None


def _parse_document_date(value: Any) -> Optional[date]:
    """Best-effort parse of a producer-supplied date/datetime string (spec
    §10: Graph's ``lastModifiedDateTime``, ISO-8601) into a plain
    :class:`datetime.date`. Returns ``None`` for anything unparseable —
    the caller treats a missing/invalid date exactly like an absent one
    (§10's "null dates" rule), never raises on malformed producer input."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None
