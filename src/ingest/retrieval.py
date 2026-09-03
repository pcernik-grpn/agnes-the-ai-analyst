"""Hybrid retrieval over Collections chunks.

Brute-force at the current scale (dozens of files): fetch the candidate chunks
for the caller's *granted* corpora, score each by IDF-weighted lexical term
overlap and — when an embedding model is installed and the chunk was embedded —
cosine similarity, fuse the two, and return the top-k with citations.
Brute-force keeps the door open for an indexed strategy (DuckDB
``vss``/HNSW) later behind this same ``search`` interface.

RBAC is the caller's responsibility: pass only ``corpus_ids`` the caller may
access. Empty ``corpus_ids`` → empty result (fail-closed) — never "search all".

Scoring (#756 — tiny-corpus hybrid-search fix)
-----------------------------------------------
The naive "fraction of distinct query terms present" lexical score treats
every term as equally important, so any two chunks that happen to contain the
full query term set tie at exactly 1.0 — on a tiny corpus (a handful of
files, the common case for newly-created Collections) that tie is broken by
DB fetch order, not relevance. Fixed by:

1. IDF-weighting the lexical score over the in-memory candidate set (a chunk
   matching a term unique to it outweighs one matching only terms common to
   most candidates), normalized to the query's total IDF mass.
2. Min-max normalizing each component (lexical, cosine) across the candidate
   set before the fixed 0.5/0.5 blend, so a noisy/degenerate component can't
   silently dominate.
3. A stable ``(-score, chunk_id)`` sort key, so equal-scoring chunks resolve
   deterministically instead of by DB fetch order.
4. A calibrated ``confidence`` ("high"/"medium"/"low") derived from the
   top-vs-runner-up score margin and how many distinct source files the
   candidate set actually spans — a tiny corpus (few distinct files) or a
   thin margin can never earn "high", matching how little the ranking signal
   can be trusted at that scale.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

from src.ingest.embeddings import embed_query, embedding_capability
from src.repositories import corpus_chunks_repo, corpus_files_repo

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Default cap on the number of candidate chunks a single search loads,
#: overridable via ``knowledge.retrieval.max_candidate_chunks`` (see
#: ``_max_candidate_chunks``). P0 OOM fix, 2026-09 — see the module
#: docstring's addendum below ``search`` for the incident.
_DEFAULT_MAX_CANDIDATE_CHUNKS = 5000

# Bounds how many filename-search terms `search()` passes down to the repo
# layer — a pathologically long query must not turn into a pathologically
# long WHERE clause on the filename-fallback candidate path either.
_MAX_FILENAME_TERMS = 16


def _max_candidate_chunks() -> int:
    """The configured hard cap on chunks a single search may load.

    ``knowledge.retrieval.max_candidate_chunks``, default
    ``_DEFAULT_MAX_CANDIDATE_CHUNKS``. Deferred import + broad except
    (matching ``connectors.bigquery.extractor._get_lock_ttl_seconds``):
    this must never fail a search because instance config couldn't be
    read.
    """
    try:
        from app.instance_config import get_value

        v = get_value("knowledge", "retrieval", "max_candidate_chunks", default=_DEFAULT_MAX_CANDIDATE_CHUNKS)
        n = int(v) if v is not None else _DEFAULT_MAX_CANDIDATE_CHUNKS
        return n if n > 0 else _DEFAULT_MAX_CANDIDATE_CHUNKS
    except Exception:
        return _DEFAULT_MAX_CANDIDATE_CHUNKS


class SearchResults(list):
    """A ranked ``list[dict]`` search result, carrying whether the
    candidate scan was capped (``.capped``).

    Subclassing ``list`` — rather than a tuple or a wrapping dict — keeps
    every existing caller (``== []``, ``res[0]``, ``for r in res``,
    ``len(res)``, ``bool(res)``) working exactly as before; only a caller
    that wants the P0 cap signal reads ``.capped``
    (``app.api.knowledge_search`` / ``app.api.collections``).
    """

    def __init__(self, iterable=(), *, capped: bool = False) -> None:
        super().__init__(iterable)
        self.capped = capped


# Confidence calibration (see module docstring point 4). Deliberately
# conservative: issue #756 was filed because a 2-5 file corpus surfaced a
# wrong top match at what read as full confidence.
_MIN_FILES_FOR_MEDIUM = 3  # fewer distinct files than this → always "low"
_MIN_FILES_FOR_HIGH = 6  # fewer distinct files than this → capped at "medium"
_HIGH_MARGIN = 0.2  # top-vs-runner-up normalized-score gap required for "high"
_LOW_MARGIN = 0.05  # below this gap, ranking is effectively a toss-up → "low"


def retrieval_mode() -> str:
    """``"hybrid"`` when semantic scoring is active, else ``"lexical_only"``.

    Surfaces the silent lexical-only degradation (no ``agnes[embeddings]``
    extra installed → ``embed_query`` returns None → pure lexical ranking)
    as a response-level label. API/MCP search responses carry it as
    ``retrieval`` so a client can tell hybrid results from degraded ones
    without reading server logs (#898). Uses ``embedding_capability`` — a
    probe that never loads the model — so labeling a response can't force
    an expensive model init on requests where no ranking ran.
    """
    return "hybrid" if embedding_capability() else "lexical_only"


def _tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _idf(doc_freq: int, n_candidates: int) -> float:
    """Smoothed IDF: ``ln((N+1)/(df+1)) + 1`` — always positive, never divides
    by zero, and degrades to a flat 1.0 weight for a term present in every
    candidate (no discriminating power) up to a high weight for a term unique
    to one candidate."""
    return math.log((n_candidates + 1) / (doc_freq + 1)) + 1.0


def _lexical_scores(q_terms: set[str], texts: List[str]) -> List[float]:
    """IDF-weighted lexical overlap for each candidate text, normalized to the
    query's total IDF mass (so the result stays roughly in ``[0, 1]``).

    Terms rare across the candidate set carry more weight than terms common
    to nearly every candidate, so a chunk matching one distinctive term
    outranks a chunk matching only common terms — the core #756 fix.
    """
    n = len(texts)
    if not q_terms or n == 0:
        return [0.0] * n
    tokensets = [set(_tokenize(t)) for t in texts]
    idf = {term: _idf(sum(1 for toks in tokensets if term in toks), n) for term in q_terms}
    total_mass = sum(idf.values()) or 1.0
    return [sum(idf[t] for t in q_terms if t in toks) / total_mass for toks in tokensets]


def _minmax_normalize(values: List[float]) -> List[float]:
    """Min-max normalize to ``[0, 1]`` across the candidate set.

    Degenerate cases (no values, or every value identical) can't divide by a
    zero range: an empty list normalizes to itself, and an all-equal set
    normalizes to 1.0 when the shared value is positive (a real, uniform
    signal) or 0.0 when it's all zero (no signal at all).
    """
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return [1.0 if v > 0 else 0.0 for v in values]
    return [(v - lo) / (hi - lo) for v in values]


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _confidence(sorted_scores: List[float], distinct_files: int) -> str:
    """Calibrated confidence label for a ranked result set.

    Driven by two signals: the top-vs-runner-up normalized-score margin (a
    thin margin means the ranking is close to arbitrary) and how many
    distinct source files the candidate set spans (a tiny corpus can't
    reliably discriminate "the best" document regardless of margin — the
    #756 failure mode). A single-candidate result has no runner-up to
    compare against, so its own score stands in for the margin.
    """
    if not sorted_scores:
        return "low"
    margin = sorted_scores[0] - sorted_scores[1] if len(sorted_scores) > 1 else sorted_scores[0]
    if distinct_files < _MIN_FILES_FOR_MEDIUM:
        return "low"
    if margin >= _HIGH_MARGIN and distinct_files >= _MIN_FILES_FOR_HIGH:
        return "high"
    if margin >= _LOW_MARGIN:
        return "medium"
    return "low"


def rank_chunks(
    chunks: List[Dict[str, Any]],
    query: str,
    *,
    k: int = 10,
) -> tuple[List[tuple[float, Dict[str, Any]]], str]:
    """Score+rank a candidate chunk set (the #756 hybrid pipeline).

    Returns ``(top, confidence)`` where ``top`` is up to ``k``
    ``(score, chunk)`` pairs, sorted by fused score descending with a stable
    chunk-id tie-break. Pure scoring — no repo access, no RBAC, no filename
    resolution — so both the server's ``search()`` and the offline
    ``src.search.local`` reader can share the exact same ranking behavior
    over their respective candidate sets.
    """
    q_terms = set(_tokenize(query))
    q_vec: Optional[List[float]] = embed_query(query)  # None when extra absent

    # Raw, un-normalized components over the FULL candidate set (not just the
    # ones with a hit) — IDF needs the non-matching candidates to correctly
    # judge how rare/common each query term is, and the confidence
    # calibration needs the full distinct-file count for the corpus.
    texts = [ch.get("text", "") for ch in chunks]
    lex_raw = _lexical_scores(q_terms, texts)
    vec_raw = [0.0] * len(chunks)
    if q_vec is not None:
        for i, ch in enumerate(chunks):
            emb = ch.get("embedding")
            if emb:
                vec_raw[i] = max(0.0, _cosine(q_vec, list(emb)))

    lex_norm = _minmax_normalize(lex_raw)
    vec_norm = _minmax_normalize(vec_raw) if q_vec is not None else None

    fused: List[float] = []
    for i in range(len(chunks)):
        if vec_norm is not None:
            fused.append(0.5 * lex_norm[i] + 0.5 * vec_norm[i])
        else:
            fused.append(lex_norm[i])

    # Keep only candidates with a real signal (raw, not normalized — the
    # degenerate all-zero-becomes-uniform case in `_minmax_normalize` must
    # never resurrect a chunk with no actual lexical or vector match).
    scored = [(fused[i], ch) for i, ch in enumerate(chunks) if lex_raw[i] > 0 or vec_raw[i] > 0]
    # Deterministic ordering: fused score descending, then chunk id ascending
    # as a stable tie-break — no longer dependent on DB fetch order (#756).
    scored.sort(key=lambda x: (-x[0], str(x[1].get("id") or "")))

    distinct_files = len({ch.get("file_id") for ch in chunks})
    confidence = _confidence([s for s, _ in scored], distinct_files)

    return scored[:k], confidence


#: Function words that are evidence for nothing. They appear in most English
#: questions and in almost every passage, so counting them made a passage
#: containing "is" and "in" look as explanatory as a file NAMED after the
#: subject — which is how "what is in quarterly-report.md" kept missing the
#: file it names. (Devin Review on #1267.)
_QUERY_STOP_TOKENS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "file",
        "find",
        "for",
        "from",
        "get",
        "give",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "please",
        "show",
        "tell",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "you",
        "your",
    }
)

#: Filename tokens too generic to identify a file. An extension is shared by
#: every file of that type, so matching on it returns the whole corpus as
#: "hits" — noise dressed as a result.
_FILENAME_STOP_TOKENS = frozenset(
    {
        "md",
        "txt",
        "pdf",
        "csv",
        "tsv",
        "json",
        "yaml",
        "yml",
        "html",
        "htm",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
        "zip",
        "parquet",
        "log",
    }
)


def _content_terms(query: str) -> set[str]:
    """Query tokens with function words and bare file extensions stripped.

    The shared "does this word identify a FILE" filter: originally inline
    in both ``_rank_by_filename`` and ``apply_filename_fallback`` (kept in
    sync by hand); factored out when the P0 OOM fix (2026-09) added a THIRD
    consumer — ``search()`` uses it to decide, before paying for a DB round
    trip, whether the filename-fallback candidate path
    (``corpus_chunks_repo().search_by_filename``) could possibly match
    anything.
    """
    return {t for t in _tokenize(query) if t not in _FILENAME_STOP_TOKENS and t not in _QUERY_STOP_TOKENS}


def _rank_by_filename(
    chunks: List[Dict[str, Any]],
    query: str,
    filename_of: Any,
    *,
    k: int = 10,
) -> List[tuple[float, Dict[str, Any]]]:
    """Chunks whose FILE NAME matches the query, for the no-body-hit case.

    Scored by how much of the query the name accounts for, so a full
    ``quarterly-report`` beats a bare ``report``. Ordered by that overlap and
    then by ``ordinal``, so a matched file reads from its beginning rather
    than from an arbitrary chunk.

    Extensions are excluded (see ``_FILENAME_STOP_TOKENS``): ``md`` appears
    in every markdown filename, so honouring it would answer "md" with the
    entire corpus.
    """
    # The SAME content-word set the caller compares against: counting filler
    # words here let files that share a "what"/"is"/"in" with the question
    # outrank the file the question actually names, and the stricter filter
    # downstream then had nothing left to accept. (Devin Review on #1267.)
    q_terms = _content_terms(query)
    if not q_terms:
        return []

    scored: List[tuple[float, Dict[str, Any]]] = []
    for ch in chunks:
        name = filename_of(ch.get("file_id"))
        if not name:
            continue
        name_terms = {t for t in _tokenize(name) if t not in _FILENAME_STOP_TOKENS}
        overlap = q_terms & name_terms
        if overlap:
            scored.append((len(overlap) / len(q_terms), ch))

    scored.sort(key=lambda pair: (-pair[0], pair[1].get("ordinal") or 0, str(pair[1].get("id"))))
    # Uncapped: the caller filters this shortlist (a chunk whose own text
    # explains as much as the name is not a name hit) and caps afterwards.
    # Cutting to `k` first meant a large file's early chunks could fill the
    # list, get filtered out, and leave the file unfound. (Devin Review on
    # #1267.)
    return scored


#: The name pass is skipped only when some passage explains the WHOLE
#: question. A lower bar looked cheaper but broke the case the pass exists
#: for: a two-word question ("quarterly report") is half-covered by any
#: passage containing either word, and the file named after both would never
#: be considered. The cost argument does not survive contact with the line
#: above it either — `search()` has already loaded every chunk row of every
#: corpus in scope, next to which one file listing per corpus is a rounding
#: error. (Devin Review on #1267, arguing both directions across two rounds.)
_NAME_PASS_BODY_CEILING = 1.0


def apply_filename_fallback(
    chunks: List[Dict[str, Any]],
    query: str,
    filename_of: Any,
    top: List[tuple[float, Dict[str, Any]]],
    confidence: str,
    *,
    k: int = 10,
    prepare: Any = None,
) -> tuple[List[tuple[float, Dict[str, Any]]], str, set]:
    """Let file NAMES answer when they explain the question better than any body.

    Returns ``(top, confidence, filename_ids)``. Shared by the server's
    ``search()`` and the offline ``src.search.local`` reader so the two cannot
    drift — they are the online and offline halves of the same question.

    The trigger is comparative, and it took three rounds of review to get
    right. "No results at all" made the whole thing dead code wherever the
    embeddings extra is installed, because every chunk then scores a non-zero
    cosine. "No body contains ANY query word" was barely better: one stray
    ``in`` or ``the`` disabled it, which is every question phrased as a
    sentence. So both sides are scored the same way — the share of the query's
    CONTENT words (function words and bare extensions excluded) that the
    candidate accounts for — and a name only leads when it explains more of
    the question than the best passage does.

    Name hits lead; whatever the body pass found keeps the remaining slots,
    rescaled UNDER the weakest name hit. The two raw scales are unrelated
    (coverage ratios here, fused lexical/vector scores there), and a merged
    list whose scores contradict its order misleads every consumer that reads
    them. (Devin Review on #1267.)
    """
    q_terms = _content_terms(query)
    if not q_terms:
        return top, confidence, set()

    def _cover(text: str) -> float:
        return len(q_terms & set(_tokenize(text or ""))) / len(q_terms)

    # Over EVERY candidate, not just the `k` rows `rank_chunks` kept: a
    # passage that explains the whole question but ranked k+1 would otherwise
    # be invisible here and the pass would fire on a partial view.
    # (Devin Review on #1267.)
    best_body_cover = max((_cover(ch.get("text", "")) for ch in chunks), default=0.0)
    if best_body_cover >= _NAME_PASS_BODY_CEILING:
        # Some passage already explains the WHOLE question — no name can beat
        # that, so neither the pass nor the file listing `prepare` loads is
        # needed. A partially-explained question DOES pay for the listing, and
        # that is the deliberate side of the trade: the two-word case is the
        # one the feature exists for, and one listing per collection is a
        # rounding error next to the chunk rows already loaded above.
        # (Devin Review on #1267, arguing both directions across rounds.)
        return top, confidence, set()
    if prepare is not None:
        prepare()
    # The label goes to whichever explained more of the question. A passage
    # that carries the searched words stays a body hit — relabelling it would
    # misdescribe it and move it out of the order its own score earned — but
    # only while its text accounts for at least as much as its file's NAME
    # does. A chunk that happens to contain one word of a two-word question,
    # inside the file named after both, is a name hit; treating any textual
    # overlap as disqualifying let the named file lose to whichever chunk id
    # sorted first. Keyed on the text rather than on presence in `top`,
    # because with semantic scoring every chunk is in `top`.
    # (Devin Review on #1267, three rounds on this trigger.)
    name_hits = []
    for pair in _rank_by_filename(chunks, query, filename_of, k=k):
        name_cover = _cover(filename_of(pair[1].get("file_id")) or "")
        if name_cover <= best_body_cover:
            continue
        if _cover(pair[1].get("text", "")) >= name_cover:
            continue
        name_hits.append(pair)
        if len(name_hits) >= k:
            break
    if not name_hits:
        return top, confidence, set()

    filename_ids = {ch.get("id") for _s, ch in name_hits}
    rest = [pair for pair in top if pair[1].get("id") not in filename_ids]
    floor = min(s for s, _ch in name_hits)
    top_rest = max((s for s, _ch in rest), default=0.0) or 1.0
    rest = [(round(floor * 0.9 * (s / top_rest), 6), ch) for s, ch in rest]
    # `confidence` stays the body pass's own judgement — the name rows are
    # already marked by `matched_on`, and downgrading the whole response
    # misdescribes the passages that did match. The caller labels per row.
    # (Devin Review on #1267.)
    return (name_hits + rest)[:k], confidence, filename_ids


def search(
    corpus_ids: List[str],
    query: str,
    *,
    k: int = 10,
) -> List[Dict[str, Any]]:
    """Return up to ``k`` ranked chunks from the given corpora, with citations.

    Fail-closed: empty ``corpus_ids`` or blank query → ``[]``.

    Bounded (P0 OOM fix, 2026-09): candidate SELECTION happens in SQL, not
    Python. This used to call ``list_for_corpora``, which loaded EVERY chunk
    row across the caller's whole granted corpus set before scoring a
    single one — fine at the dozens-of-files scale this module's docstring
    describes, but for an admin (or any caller with a wide grant) on a
    production corpus it turned one search into a 10M-row / 12GB sequential
    scan and OOM-killed the process (13 restarts in 75 minutes while users
    retried). ``corpus_chunks_repo().search_candidates()`` now does lexical
    candidate selection IN SQL — Postgres full-text search, ranked; a plain
    ILIKE prefilter on the frozen DuckDB backend — under a hard ``LIMIT``
    (``knowledge.retrieval.max_candidate_chunks``, default 5000, see
    ``_max_candidate_chunks``): the process never holds more than that many
    rows in memory regardless of corpus size. ``rank_chunks``'s IDF-lexical
    + cosine fusion is unchanged; it just runs over this bounded set.

    Trade-off, by construction: a query with literally no shared vocabulary
    in any candidate's body text can no longer be found by embedding
    similarity alone once a corpus exceeds the cap — body candidate
    SELECTION is lexical-first (an unindexed vector scan over an unbounded
    corpus is exactly the memory problem this fixes; see
    ``CorpusChunksPgRepository.search_candidates``). The filename fallback
    is unaffected by that trade-off: ``search_by_filename`` is a SEPARATE
    bounded candidate path keyed on the file's NAME rather than its body,
    so "what is in quarterly-report.md?" still finds a file whose body
    shares no words with the question.
    """
    if not corpus_ids or not (query or "").strip():
        return []

    chunks_repo = corpus_chunks_repo()
    cap = _max_candidate_chunks()
    body_chunks = chunks_repo.search_candidates(corpus_ids, query, limit=cap)
    capped = len(body_chunks) >= cap

    # Filename-match candidates, kept SEPARATE from `body_chunks` (see the
    # docstring above / `search_by_filename`'s own docstring) — gated on the
    # same "does the query even have a content word" check
    # `apply_filename_fallback` uses, applied here FIRST so a query that
    # cannot possibly trigger the fallback (bare stopwords/extensions)
    # doesn't pay for the extra bounded query.
    terms = list(_content_terms(query))[:_MAX_FILENAME_TERMS]
    name_chunks = chunks_repo.search_by_filename(corpus_ids, terms, limit=cap) if terms else []
    capped = capped or len(name_chunks) >= cap

    seen_ids = {ch.get("id") for ch in body_chunks}
    fallback_pool = body_chunks + [ch for ch in name_chunks if ch.get("id") not in seen_ids]
    if not fallback_pool:
        return []

    top, confidence = rank_chunks(body_chunks, query, k=k)

    # Resolve filenames for citations, one file at a time and cached — a
    # normal search cites at most `k` of them. The bulk listing below is
    # loaded ONLY when the name pass actually runs, so an ordinary search
    # never pays for every collection's file list. (Devin Review on #1267,
    # both halves: the per-file loop was an N+1 for the name pass, and
    # loading everything up front was a tax on the searches that do not need
    # it.)
    cf_repo = corpus_files_repo()
    name_cache: Dict[str, Optional[str]] = {}
    names_bulk_loaded = False

    def _load_all_names() -> None:
        nonlocal names_bulk_loaded
        if names_bulk_loaded:
            return
        names_bulk_loaded = True
        for cid in corpus_ids:
            for row in cf_repo.list_for_corpus(cid):
                name_cache.setdefault(row.get("id"), row.get("filename"))

    def _filename(file_id: str) -> Optional[str]:
        if file_id not in name_cache:
            row = cf_repo.get(file_id)
            name_cache[file_id] = row.get("filename") if row else None
        return name_cache[file_id]

    # Names are only consulted when they beat the body — see
    # `apply_filename_fallback`, which the offline reader shares. Runs over
    # `fallback_pool` (body candidates + filename candidates), never over an
    # unbounded full corpus listing.
    top, confidence, filename_ids = apply_filename_fallback(
        fallback_pool, query, _filename, top, confidence, k=k, prepare=_load_all_names
    )

    results: List[Dict[str, Any]] = []
    for score, ch in top:
        results.append(
            {
                "chunk_id": ch.get("id"),
                "corpus_id": ch.get("corpus_id"),
                "file_id": ch.get("file_id"),
                "filename": _filename(ch.get("file_id")),
                "ordinal": ch.get("ordinal"),
                "section_path": ch.get("section_path"),
                "text": ch.get("text"),
                "score": round(float(score), 4),
                # A name match is a hint, not evidence — that row says so,
                # while a passage that really matched keeps the body pass's
                # own judgement. (Devin Review on #1267.)
                "confidence": "low" if ch.get("id") in filename_ids else confidence,
                # How THIS hit was found. The combined search caps a bucket of
                # name-only hits (see `src/search/unified.py`): min-max
                # normalization makes any bucket's top hit 1.0, so without the
                # label a weak name match arrives looking exactly as strong as
                # a document that genuinely contains the words.
                "matched_on": "filename" if ch.get("id") in filename_ids else "body",
            }
        )
    return SearchResults(results, capped=capped)
