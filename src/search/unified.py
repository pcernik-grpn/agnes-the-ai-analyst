"""Unified knowledge search (K2, #797) — one query across several engines.

Thin fan-out over the EXISTING search surfaces; none is modified here:

1. Collections chunks — ``src.ingest.retrieval.search`` (hybrid lexical+vector)
2. Knowledge base — ``knowledge_repo().search`` (FTS/BM25 or ILIKE fallback)
3. Table catalog cards — lexical term overlap over ``table_registry`` rows;
   a table hit returns a *pivot hint* (query it with SQL), never rows.
4. Metrics — lexical term overlap over the semantic-layer ``metric_repo`` rows
   (name/display_name/description/synonyms); caller-prefiltered by table RBAC.
5. Glossary — ``glossary_repo().search`` (FTS/BM25 or ILIKE fallback); public,
   no RBAC, so fetched inside rather than passed in (#1108).

RBAC is the caller's responsibility: pass only granted ``corpus_ids`` /
``user_groups`` / ``granted_domains`` / pre-filtered ``tables`` / pre-filtered
``metrics``. Glossary is the one public source. An empty
grant set for a source contributes nothing (fail-closed), never "search all".
``user_groups`` / ``granted_domains`` follow the knowledge repo's convention:
``None`` means *no filter* (privileged viewer), ``[]`` means *no grants* —
the knowledge source is skipped only when BOTH are empty lists.

Merging: scores are min-max normalized WITHIN each source (the engines'
score scales are incomparable), then interleaved by normalized score with a
deterministic tie-break (type, then id) so equal-score runs are stable.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return _TOKEN.findall((text or "").lower())


def _chunk_search(corpus_ids: List[str], query: str, k: int = 10) -> List[Dict[str, Any]]:
    from src.ingest.retrieval import search

    return search(corpus_ids, query, k=k)


def _knowledge_search(query: str, **kw: Any) -> List[Dict[str, Any]]:
    from src.repositories import knowledge_repo

    return knowledge_repo().search(query, **kw)


def _glossary_search(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    from src.repositories import glossary_repo

    return glossary_repo().search(query, limit=limit)


def _dedupe_by_document(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One hit per document, keeping its best-scoring passage.

    Called only when another source also matched — see the caller, which gates
    this and the name cap on the same question for the same reason.

    Ranking is unchanged: the surviving hit is the one the ranker already put
    first for that document, so the ORDER of documents is exactly what it was
    with the repeats removed. Hits with no `file_id` (a source that is not
    chunked) pass through untouched rather than collapsing into one bucket.
    """
    best: Dict[Any, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []
    for h in hits:
        key = h.get("file_id")
        if key is None:
            out.append(h)
            continue
        prev = best.get(key)
        if prev is None:
            best[key] = h
            out.append(h)
        elif (h.get("score") or 0) > (prev.get("score") or 0):
            out[out.index(prev)] = h
            best[key] = h
    return out


def _is_exact_identifier(query: str, *names: Any) -> bool:
    """Did the caller type this thing's NAME, rather than words about it?

    Compared on tokens, so `bi_chargeability`, `bi chargeability` and
    `BI-Chargeability` are the same question. Used to pin known-item hits
    above the relevance ranking — see the note in `unified_search`.
    """
    q = _tokenize(query)
    if not q:
        return False
    return any(_tokenize(str(n)) == q for n in names if n)


def _plain(row: Dict[str, Any], field: str) -> str:
    """``row[field]`` as plain text, honoring the row's stored dialect."""
    from app.markdown_render import render_plain, stores_html

    return render_plain(row.get(field), html_source=stores_html(row)) or ""


def _minmax(scores: List[float]) -> List[float]:
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if hi <= lo:
        return [1.0] * len(scores)
    return [(s - lo) / (hi - lo) for s in scores]


def _table_scores(query: str, tables: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Lexical term-overlap scoring over name + description + column names."""
    q_terms = set(_tokenize(query))
    if not q_terms:
        return []
    out: List[Dict[str, Any]] = []
    for t in tables:
        cols = ""
        cj = t.get("columns_json")
        if cj:
            try:
                cols = " ".join(str(c.get("name", "")) for c in json.loads(cj))
            except (ValueError, TypeError, AttributeError):
                cols = ""
        hay = set(_tokenize(f"{t.get('name', '')} {t.get('description', '')} {cols}"))
        overlap = len(q_terms & hay)
        if overlap == 0:
            continue
        table_id = t.get("id")
        out.append(
            {
                "type": "table",
                "table_id": table_id,
                "name": t.get("name"),
                "description": t.get("description"),
                "score": overlap / len(q_terms),
                "pivot_hint": (f"structured data — query with SQL via `agnes query`, table id: {table_id}"),
            }
        )
    out.sort(key=lambda h: (-h["score"], h["table_id"] or ""))
    return out


def _metric_scores(query: str, metrics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Lexical term-overlap over name + display_name + description + synonyms.

    Mirrors ``_table_scores`` — ``metric_repo`` has no ``search`` on either
    backend, so ranking is inline. Drops zero-overlap, ``score = |q∩hay|/|q|``,
    deterministic ``(-score, id)`` sort. Caller must pre-filter ``metrics`` by
    table-access RBAC (see app/api/knowledge_search.py); this fn only ranks.
    """
    q_terms = set(_tokenize(query))
    if not q_terms:
        return []
    out: List[Dict[str, Any]] = []
    for m in metrics:
        syn = m.get("synonyms") or []
        syn_text = " ".join(str(s) for s in syn) if isinstance(syn, list) else str(syn)
        hay = set(_tokenize(f"{m.get('name', '')} {m.get('display_name', '')} {m.get('description', '')} {syn_text}"))
        overlap = len(q_terms & hay)
        if overlap == 0:
            continue
        mid = m.get("id")
        out.append(
            {
                "type": "metric",
                "id": mid,
                "name": m.get("name"),
                "display_name": m.get("display_name"),
                "category": m.get("category"),
                "description": m.get("description"),
                "score": overlap / len(q_terms),
            }
        )
    out.sort(key=lambda h: (-h["score"], h["id"] or ""))
    return out


def _plugin_scores(query: str, plugins: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Lexical term-overlap over name + display name + description + category.

    Mirrors ``_table_scores`` / ``_metric_scores``: drops zero-overlap, scores
    ``|q∩hay|/|q|``, deterministic ``(-score, id)`` sort. Plugins were the one
    installable thing the combined search could not find — searching an
    installed plugin by name returned a metric and some documents and never the
    plugin itself (#1956 item 5).

    The caller must pre-filter by ``marketplace_plugin`` grants, exactly as it
    does for tables; this fn only ranks. ``id`` is the marketplace-scoped
    ``<marketplace_id>/<name>`` path, which is both the grant's resource id and
    the detail page's URL.
    """
    q_terms = set(_tokenize(query))
    if not q_terms:
        return []
    out: List[Dict[str, Any]] = []
    for p in plugins:
        mid, name = p.get("marketplace_id"), p.get("name")
        if not name:
            continue
        hay = set(_tokenize(f"{name} {p.get('display_name', '')} {p.get('description', '')} {p.get('category', '')}"))
        overlap = len(q_terms & hay)
        if overlap == 0:
            continue
        path = f"{mid}/{name}" if mid else str(name)
        out.append(
            {
                "type": "plugin",
                "id": path,
                "slug": name,
                "name": name,
                "display_name": p.get("display_name") or name,
                "description": p.get("description"),
                "category": p.get("category"),
                "marketplace_id": mid,
                "score": overlap / len(q_terms),
            }
        )
    out.sort(key=lambda h: (-h["score"], h["id"] or ""))
    return out


def unified_search(
    query: str,
    *,
    corpus_ids: List[str],
    user_groups: Optional[List[str]],
    granted_domains: Optional[List[str]],
    tables: List[Dict[str, Any]],
    metrics: Optional[List[Dict[str, Any]]] = None,
    plugins: Optional[List[Dict[str, Any]]] = None,
    chunk_hits: Optional[List[Dict[str, Any]]] = None,
    k: int = 10,
) -> List[Dict[str, Any]]:
    """Fan the query out over all sources and return one ranked list.

    ``metrics`` must be pre-filtered by the caller for table-access RBAC (like
    ``tables``); it defaults to ``None`` (treated as empty) so existing callers
    keep working. Glossary is fetched inside via ``_glossary_search`` — it has
    no RBAC (public to any authenticated user), so there is nothing to pre-filter.

    The returned list carries a ``.capped`` attribute (P0 OOM fix, 2026-09):
    ``True`` when the chunk leg's bounded candidate scan
    (``src.ingest.retrieval.search``) hit its configured limit, meaning some
    matching chunks may have been left out. Additive — every existing caller
    that only iterates/indexes/compares the return value is unaffected; see
    ``src.ingest.retrieval.SearchResults``, which this reuses. When the
    caller supplies ``chunk_hits`` (below), the flag is read off THAT list
    instead — pass a ``SearchResults`` to propagate it, a plain list reads
    as not capped.

    ``chunk_hits`` (#2151) lets a caller pre-resolve the Collections leg
    itself instead of this function calling ``src.ingest.retrieval.search``
    (via ``_chunk_search``) internally — ``app.api.knowledge_search`` does,
    so it can catch a chunk-engine failure (a huge corpus's search timing
    out or exhausting memory) and degrade that ONE leg to an empty list
    without losing the other legs' results, the same way a caught
    exception anywhere else in this function would otherwise take the
    WHOLE combined search down with it. ``None`` (the default) preserves
    the original behavior exactly: every existing caller (this module's own
    tests) fetches chunks here, keyed on ``corpus_ids``. An explicit ``[]``
    (the degraded case) means "no chunk results", not "fetch normally" —
    checked via ``is not None``, not truthiness.
    """
    if not (query or "").strip():
        return []

    buckets: List[List[Dict[str, Any]]] = []

    # Cap for buckets whose top hit is strong only relative to itself. Defined
    # here because the chunk bucket needs it too: `_minmax` maps any bucket's
    # best hit to 1.0, so a fallback that matched only FILE NAMES would arrive
    # indistinguishable from a document that actually contains the words and
    # could take every slot from knowledge, glossary and tables that genuinely
    # matched. (Devin Review on #1267.)
    _sem_cap = max(2, k // 5)

    # Deduped by document before anything else looks at the bucket: the cap
    # below and `_minmax` both reason about "how many slots this bucket takes",
    # and six passages of one file are one document's worth of answer, not six.
    if chunk_hits is not None:
        _raw_chunk_hits: List[Dict[str, Any]] = chunk_hits
    else:
        _raw_chunk_hits = _chunk_search(corpus_ids, query, k=k) if corpus_ids else []
    candidates_capped = bool(getattr(_raw_chunk_hits, "capped", False))
    resolved_chunk_hits = [dict(h, type="chunk") for h in _raw_chunk_hits]
    # Cap the NAME-matched hits inside the bucket, rather than only capping a
    # bucket that is entirely name hits: since the fallback keeps body hits
    # and merely rescales them, an all-filename bucket is now rare and an
    # all-or-nothing test would almost never fire. Body hits are untouched.
    # (Devin Review on #1267.)
    buckets.append(resolved_chunk_hits)

    knowledge_hits: List[Dict[str, Any]] = []
    # None = privileged viewer (no filter); [] = zero grants → fail-closed.
    knowledge_enabled = user_groups is None or granted_domains is None or bool(user_groups or granted_domains)
    if knowledge_enabled:
        for rank, item in enumerate(
            _knowledge_search(
                query,
                exclude_personal=True,
                user_groups=user_groups,
                granted_domains=granted_domains,
                limit=k,
            )
        ):
            content = item.get("content") or ""
            knowledge_hits.append(
                {
                    "type": "knowledge",
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "snippet": content[:280],
                    "domain": item.get("domain"),
                    # BM25 rank order only (the repo returns no score in the
                    # ILIKE fallback) — decay by position, top hit = 1.0.
                    "score": 1.0 / (1 + rank),
                }
            )
    buckets.append(knowledge_hits)
    buckets.append(_table_scores(query, tables)[:k] if tables else [])

    # Same cap, same reason, for the semantic buckets: a _minmax-normalized
    # top-1 hit (score=1.0) must not crowd out more than a proportional share
    # of the final k slots. 2 slots at k=10, scaling with k. The legacy buckets
    # keep their [:k] cap.
    buckets.append(_metric_scores(query, metrics or [])[:_sem_cap] if metrics else [])
    # Same cap and the same pre-filtered-by-the-caller contract as metrics.
    buckets.append(_plugin_scores(query, plugins or [])[:_sem_cap] if plugins else [])

    # Glossary is public — fetched inside, not caller-prefiltered. BM25 score is
    # NULL in the ILIKE fallback, so decay by rank like the knowledge bucket.
    glossary_hits: List[Dict[str, Any]] = []
    for rank, g in enumerate(_glossary_search(query, limit=k)[:_sem_cap]):
        glossary_hits.append(
            {
                "type": "glossary",
                "id": g.get("id"),
                "term": g.get("term"),
                # Flattened here, unlike metric descriptions (which arrive
                # already projected from the caller): glossary rows are fetched
                # inside this function, so there is no caller to do it. The
                # column holds definitions imported verbatim from an external
                # catalog, often rich HTML, and this hit is read by agents
                # through the MCP `search` tool. Flatten before truncating, or
                # the cut lands mid-tag.
                "definition": _plain(g, "definition")[:280],
                "score": 1.0 / (1 + rank),
            }
        )
    buckets.append(glossary_hits)

    # Cap a name-only chunk bucket ONLY when something else actually matched.
    # The cap exists to stop weak name hits crowding out real matches from the
    # other sources; when there are no other matches it would just throw away
    # the answer to the query that motivated the fallback — "what is in
    # quarterly-report.md?" deserves more than two chunks of that file when
    # nothing else in the instance matched. (Devin Review on #1267.)
    # One hit per document, under exactly the same condition and for exactly
    # the same reason as the cap below it. Six chunks of one file arrive as six
    # separately-ranked hits, `_minmax` spreads them from 1.0 downward, and
    # they take six of the caller's k slots — which in the web combobox (k=8)
    # read as "the same document repeated up to 6x, and the thing I searched
    # for is missing" (#1956 item 5).
    #
    # But when the chunk bucket is ALL that matched, collapsing it is the same
    # mistake the cap is careful not to make: "what is in quarterly-report.md?"
    # is answered by that file's passages, and there is nothing they could be
    # crowding out. So both narrowings are gated on the same question — did
    # anything else match at all — and a lone document keeps every passage.
    if any(bucket for bucket in buckets[1:]):
        buckets[0] = _dedupe_by_document(buckets[0])
        named = [h for h in buckets[0] if h.get("matched_on") == "filename"]
        if named:
            keep = {id(h) for h in named[:_sem_cap]}
            buckets[0] = [h for h in buckets[0] if h.get("matched_on") != "filename" or id(h) in keep]

    merged: List[Dict[str, Any]] = []
    for bucket in buckets:
        norms = _minmax([h["score"] for h in bucket])
        for h, n in zip(bucket, norms):
            merged.append({**h, "score": round(n, 4)})

    # ── Known-item search beats relevance ranking ────────────────────────
    # `_minmax` rescales each bucket to its OWN range, so every non-empty
    # bucket contributes a hit at exactly 1.0 no matter how weakly it matched:
    # a slide deck that says "our BI stack" arrived at 1.0 beside the table
    # actually named `bi_chargeability`. The tie then broke on `h["type"]`
    # alphabetically — chunk, glossary, knowledge, metric, plugin, table — which
    # is not a relevance order at all, it is the alphabet, and it put the two
    # kinds of thing a caller searches BY NAME dead last. Hence "searching a
    # table name returns only unrelated documents" (#1956 item 5).
    #
    # When the caller typed something's name, that is not a relevance question
    # and it should not be answered with one. Such hits are pinned above the
    # ranking; everything below it is unchanged, including the alphabetical
    # tie-break, which is still the deterministic order the tests pin.
    for h in merged:
        h["exact"] = _is_exact_identifier(
            query,
            h.get("name"),
            h.get("table_id") if h.get("type") == "table" else None,
            h.get("term") if h.get("type") == "glossary" else None,
            h.get("slug") if h.get("type") == "plugin" else None,
        )

    merged.sort(
        key=lambda h: (
            not h["exact"],
            -h["score"],
            h["type"],
            str(h.get("chunk_id") or h.get("id") or h.get("table_id") or ""),
        )
    )
    from src.ingest.retrieval import SearchResults

    return SearchResults(merged[:k], capped=candidates_capped)
