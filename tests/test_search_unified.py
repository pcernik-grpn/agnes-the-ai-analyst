"""unified_search — fan-out over chunks + knowledge + tables + metrics + glossary (K2, #1108)."""

from __future__ import annotations

from unittest.mock import patch

TABLES = [
    {"id": "t_orders", "name": "orders", "description": "customer orders and revenue", "columns_json": None},
    {"id": "t_web", "name": "web_sessions", "description": "web analytics sessions", "columns_json": None},
]

METRICS = [
    {
        "id": "finance/mrr",
        "name": "mrr",
        "display_name": "Monthly Recurring Revenue",
        "description": "normalized monthly subscription revenue",
        "synonyms": ["MRR", "recurring revenue"],
        "category": "finance",
    },
    {
        "id": "product/wau",
        "name": "wau",
        "display_name": "Weekly Active Users",
        "description": "distinct users with a session in 7 days",
        "synonyms": ["WAU"],
        "category": "product",
    },
]


def _fake_chunks(corpus_ids, query, k=10):
    if not corpus_ids:
        return []
    return [
        {
            "chunk_id": "ch1",
            "corpus_id": "c1",
            "file_id": "f1",
            "filename": "billing.md",
            "ordinal": 0,
            "section_path": None,
            "text": "invoices are monthly",
            "score": 0.9,
            "confidence": "high",
        }
    ]


def _fake_knowledge(query, **kw):
    if not kw.get("granted_domains") and not kw.get("user_groups"):
        return []
    return [
        {
            "id": "ki1",
            "title": "Billing policy",
            "content": "We invoice monthly in EUR.",
            "domain": "finance",
        }
    ]


def _no_glossary(query, limit=10):
    """Default glossary mock — empty, so the base tests keep their 3-type shape."""
    return []


def _fake_glossary(query, limit=10):
    return [{"id": "g_mrr", "term": "Recurring revenue", "definition": "Revenue that recurs each period."}]


def test_merges_all_five_sources():
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", _fake_knowledge),
        patch("src.search.unified._glossary_search", _fake_glossary),
    ):
        hits = unified_search(
            "invoices orders revenue",
            corpus_ids=["c1"],
            user_groups=["g1"],
            granted_domains=["d1"],
            tables=TABLES,
            metrics=METRICS,
            k=10,
        )
    types = {h["type"] for h in hits}
    assert types == {"chunk", "knowledge", "table", "metric", "glossary"}
    table_hit = next(h for h in hits if h["type"] == "table")
    assert table_hit["table_id"] == "t_orders"
    assert "agnes query" in table_hit["pivot_hint"]
    metric_hit = next(h for h in hits if h["type"] == "metric")
    assert metric_hit["id"] == "finance/mrr"
    assert metric_hit["display_name"] == "Monthly Recurring Revenue"
    glossary_hit = next(h for h in hits if h["type"] == "glossary")
    assert glossary_hit["term"] == "Recurring revenue"
    # The DEFINITION text rides along on both types, not just the name. The
    # header search dropdown renders it as a second line under the hit
    # (definitionFor() in global_search.js) so that looking a term up finishes
    # in the panel instead of costing a page load — drop these fields and the
    # dropdown silently degrades back to a bare link.
    assert glossary_hit["definition"] == "Revenue that recurs each period."
    assert metric_hit["description"]


def test_glossary_is_public_independent_of_grants():
    """Glossary is fetched inside (no RBAC), so it appears even with zero grants
    where the knowledge source is fail-closed."""
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", _fake_knowledge),
        patch("src.search.unified._glossary_search", _fake_glossary),
    ):
        hits = unified_search("revenue", corpus_ids=[], user_groups=[], granted_domains=[], tables=[], metrics=[], k=10)
    types = {h["type"] for h in hits}
    assert "glossary" in types  # public
    assert "knowledge" not in types  # fail-closed with zero grants


def test_metrics_absent_when_not_passed():
    """Metric RBAC pre-filter is the caller's job — an empty/omitted metrics list
    yields no metric hits."""
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", _fake_knowledge),
        patch("src.search.unified._glossary_search", _no_glossary),
    ):
        hits = unified_search("revenue", corpus_ids=[], user_groups=[], granted_domains=[], tables=[], metrics=[], k=10)
        # omitted entirely (default None) behaves the same
        hits2 = unified_search("revenue", corpus_ids=[], user_groups=[], granted_domains=[], tables=[], k=10)
    assert not any(h["type"] == "metric" for h in hits)
    assert not any(h["type"] == "metric" for h in hits2)


def test_fail_closed_per_source():
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", _fake_knowledge),
        patch("src.search.unified._glossary_search", _no_glossary),
    ):
        hits = unified_search(
            "invoices", corpus_ids=[], user_groups=[], granted_domains=[], tables=[], metrics=[], k=10
        )
    assert hits == []


def test_blank_query_returns_empty():
    from src.search.unified import unified_search

    assert unified_search("  ", corpus_ids=["c1"], user_groups=["g"], granted_domains=["d"], tables=TABLES) == []


def test_k_caps_results_and_order_deterministic():
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", _fake_knowledge),
        patch("src.search.unified._glossary_search", _fake_glossary),
    ):
        a = unified_search(
            "invoices orders revenue",
            corpus_ids=["c1"],
            user_groups=["g"],
            granted_domains=["d"],
            tables=TABLES,
            metrics=METRICS,
            k=3,
        )
        b = unified_search(
            "invoices orders revenue",
            corpus_ids=["c1"],
            user_groups=["g"],
            granted_domains=["d"],
            tables=TABLES,
            metrics=METRICS,
            k=3,
        )
    assert len(a) == 3
    assert a == b


def test_table_scoring_prefers_term_overlap():
    from src.search.unified import _table_scores

    scored = _table_scores("customer orders revenue", TABLES)
    assert scored[0]["table_id"] == "t_orders"
    assert scored[0]["score"] > 0


def test_metric_scoring_prefers_term_overlap():
    from src.search.unified import _metric_scores

    scored = _metric_scores("monthly recurring revenue", METRICS)
    assert scored[0]["id"] == "finance/mrr"
    assert scored[0]["score"] > 0
    # matches on a synonym too
    syn = _metric_scores("wau", METRICS)
    assert syn and syn[0]["id"] == "product/wau"


def test_semantic_buckets_capped_so_legacy_types_not_displaced():
    """Metric + glossary buckets are capped at max(2, k//5) so their top-1
    _minmax score of 1.0 cannot crowd out legacy chunk/knowledge/table hits."""
    from src.search.unified import unified_search

    # Build many matching metrics and glossary terms to stress the cap.
    many_metrics = [
        {
            "id": f"cat/m{i}",
            "name": f"metric{i}",
            "display_name": f"Metric {i}",
            "description": "revenue orders",
            "synonyms": [],
            "category": "cat",
        }
        for i in range(10)
    ]

    def many_glossary(query, limit=10):
        return [{"id": f"g{i}", "term": f"Term {i}", "definition": "revenue"} for i in range(limit)]

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", _fake_knowledge),
        patch("src.search.unified._glossary_search", many_glossary),
    ):
        hits = unified_search(
            "revenue orders",
            corpus_ids=["c1"],
            user_groups=["g"],
            granted_domains=["d"],
            tables=TABLES,
            metrics=many_metrics,
            k=10,
        )

    by_type: dict = {}
    for h in hits:
        by_type.setdefault(h["type"], 0)
        by_type[h["type"]] += 1

    # Cap is max(2, 10//5) = 2 — neither new bucket may take more than 2 slots.
    assert by_type.get("metric", 0) <= 2
    assert by_type.get("glossary", 0) <= 2
    # Legacy types must still appear.
    assert by_type.get("chunk", 0) >= 1
    assert by_type.get("table", 0) >= 1


def test_none_grants_mean_unfiltered_privileged_viewer():
    """None (admin) must NOT be treated as fail-closed — repo gets None filters."""
    from src.search.unified import unified_search

    captured = {}

    def spy_knowledge(query, **kw):
        captured.update(kw)
        return [{"id": "ki1", "title": "T", "content": "C", "domain": "d"}]

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", spy_knowledge),
        patch("src.search.unified._glossary_search", _no_glossary),
    ):
        hits = unified_search(
            "invoices", corpus_ids=["c1"], user_groups=None, granted_domains=None, tables=[], metrics=[], k=5
        )
    assert any(h["type"] == "knowledge" for h in hits)
    assert captured["user_groups"] is None
    assert captured["granted_domains"] is None


# ── #1956 item 5: one document must not take the whole result list ──────────
#
# Reported as two symptoms with one cause: searching a table name returned
# "only unrelated documents — the same document repeated up to 6x", and the
# table that actually carried the name never appeared. A document is chunked,
# every chunk is scored separately, and `_minmax` maps the bucket's best hit to
# 1.0 — so six passages of one file arrive as six near-perfect hits and take
# the slots the table hit needed. Deduplicating by document fixes the visible
# repetition and the missing table in one move.


def _chunks_of_one_file(n):
    """One document, n matching passages — the shape that crowded the list.

    The web combobox asks for k=8, so eight passages of a single file is
    exactly enough to fill the result list on their own.
    """

    def _search(corpus_ids, query, k=10):
        return [
            {
                "chunk_id": f"ch{i}",
                "corpus_id": "c1",
                "file_id": "f_handbook",
                "filename": "handbook.md",
                "ordinal": i,
                "section_path": None,
                "text": f"passage {i} mentioning bi_chargeability in passing",
                "score": 0.9 - i * 0.01,
                "confidence": "high",
                "matched_on": "body",
            }
            for i in range(n)
        ]

    return _search


_six_chunks_of_one_file = _chunks_of_one_file(6)


CHARGEABILITY_TABLES = [
    {
        "id": "bi_chargeability",
        "name": "bi_chargeability",
        "description": "consultant chargeability by week",
        "columns_json": None,
    },
]


def test_one_document_yields_one_hit_not_one_per_chunk():
    """When another source also matched, a document contributes ONE hit.

    A table is in play here on purpose. Dedup is gated on "did anything else
    match at all", the same question the name cap beside it asks, so a lone
    document is deliberately left alone — see the test below.
    """
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _six_chunks_of_one_file),
        patch("src.search.unified._knowledge_search", lambda q, **kw: []),
        patch("src.search.unified._glossary_search", lambda q, limit=10: []),
    ):
        hits = unified_search(
            "bi_chargeability",
            corpus_ids=["c1"],
            user_groups=None,
            granted_domains=None,
            tables=CHARGEABILITY_TABLES,
            k=8,
        )

    chunks = [h for h in hits if h["type"] == "chunk"]
    assert len(chunks) == 1, f"one document should contribute one hit, got {len(chunks)}"
    # The surviving hit is the document's BEST passage, not an arbitrary one.
    assert chunks[0]["chunk_id"] == "ch0"


def test_a_lone_document_keeps_every_passage():
    """Nothing else matched, so there is nothing the passages could crowd out.

    This is the guarantee #1267 was written for — "what is in this file?"
    deserves more than one passage of it when the file is the only answer in
    the instance — and deduping unconditionally would silently take it back.
    Dedup and the name cap are gated on the same condition precisely so this
    case survives both.
    """
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _six_chunks_of_one_file),
        patch("src.search.unified._knowledge_search", lambda q, **kw: []),
        patch("src.search.unified._glossary_search", lambda q, limit=10: []),
    ):
        hits = unified_search(
            "bi_chargeability",
            corpus_ids=["c1"],
            user_groups=None,
            granted_domains=None,
            tables=[],
            k=8,
        )

    chunks = [h for h in hits if h["type"] == "chunk"]
    assert len(chunks) > 1, f"a lone document was collapsed to {len(chunks)} hit(s)"


def test_a_matching_table_is_not_crowded_out_by_one_documents_chunks():
    from src.search.unified import unified_search

    with (
        # Eight passages, k=8: without dedup the document fills every slot.
        patch("src.search.unified._chunk_search", _chunks_of_one_file(8)),
        patch("src.search.unified._knowledge_search", lambda q, **kw: []),
        patch("src.search.unified._glossary_search", lambda q, limit=10: []),
    ):
        hits = unified_search(
            "bi_chargeability",
            corpus_ids=["c1"],
            user_groups=None,
            granted_domains=None,
            tables=CHARGEABILITY_TABLES,
            k=8,
        )

    assert any(h["type"] == "table" and h["table_id"] == "bi_chargeability" for h in hits), (
        f"the table named in the query is missing; got {[(h['type'], h.get('table_id') or h.get('chunk_id')) for h in hits]}"
    )


def test_distinct_documents_are_all_kept():
    """Dedup is per DOCUMENT — two files matching stay two hits."""
    from src.search.unified import unified_search

    def _two_files(corpus_ids, query, k=10):
        return [
            dict(_six_chunks_of_one_file(corpus_ids, query)[0], chunk_id="a1", file_id="fa", filename="a.md"),
            dict(_six_chunks_of_one_file(corpus_ids, query)[1], chunk_id="b1", file_id="fb", filename="b.md"),
        ]

    with (
        patch("src.search.unified._chunk_search", _two_files),
        patch("src.search.unified._knowledge_search", lambda q, **kw: []),
        patch("src.search.unified._glossary_search", lambda q, limit=10: []),
    ):
        hits = unified_search(
            "bi_chargeability", corpus_ids=["c1"], user_groups=None, granted_domains=None, tables=[], k=8
        )

    assert {h["file_id"] for h in hits if h["type"] == "chunk"} == {"fa", "fb"}


# ── #1956 item 5, second half: plugins were not a source at all ─────────────
#
# "Searching sales_proposal (an installed plugin) returns a metric and
# documents, never the plugin."

PLUGINS = [
    {
        "marketplace_id": "acme",
        "name": "sales_proposal",
        "display_name": "Sales Proposal",
        "description": "drafts a proposal from a deal record",
        "category": "sales",
    },
    {
        "marketplace_id": "acme",
        "name": "invoice_check",
        "display_name": "Invoice Check",
        "description": "validates invoices",
        "category": "finance",
    },
]


def test_an_installed_plugin_is_findable_by_name():
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", lambda *a, **k: []),
        patch("src.search.unified._knowledge_search", lambda q, **kw: []),
        patch("src.search.unified._glossary_search", lambda q, limit=10: []),
    ):
        hits = unified_search(
            "sales_proposal",
            corpus_ids=[],
            user_groups=None,
            granted_domains=None,
            tables=[],
            plugins=PLUGINS,
            k=8,
        )

    plugins = [h for h in hits if h["type"] == "plugin"]
    assert [p["name"] for p in plugins] == ["sales_proposal"]
    # Carries what the UI needs to link to the plugin's own page.
    assert plugins[0]["marketplace_id"] == "acme"
    assert plugins[0]["id"] == "acme/sales_proposal"


def test_the_named_plugin_outranks_a_metric_that_merely_mentions_it():
    """The reported symptom was a metric winning over the plugin itself."""
    from src.search.unified import unified_search

    metrics = [
        {
            "id": "sales/proposals_sent",
            "name": "proposals_sent",
            "display_name": "Proposals sent",
            "description": "count of sales proposal documents sent",
            "synonyms": [],
            "category": "sales",
        }
    ]
    with (
        patch("src.search.unified._chunk_search", lambda *a, **k: []),
        patch("src.search.unified._knowledge_search", lambda q, **kw: []),
        patch("src.search.unified._glossary_search", lambda q, limit=10: []),
    ):
        hits = unified_search(
            "sales_proposal",
            corpus_ids=[],
            user_groups=None,
            granted_domains=None,
            tables=[],
            metrics=metrics,
            plugins=PLUGINS,
            k=8,
        )

    assert hits[0]["type"] == "plugin", [(h["type"], h.get("name")) for h in hits]


def test_plugins_are_absent_when_the_caller_was_granted_none():
    """The bucket is caller-prefiltered; an empty set contributes nothing."""
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", lambda *a, **k: []),
        patch("src.search.unified._knowledge_search", lambda q, **kw: []),
        patch("src.search.unified._glossary_search", lambda q, limit=10: []),
    ):
        hits = unified_search(
            "sales_proposal", corpus_ids=[], user_groups=None, granted_domains=None, tables=[], plugins=[], k=8
        )

    assert not [h for h in hits if h["type"] == "plugin"]


# ---------------------------------------------------------------------------
# #2151: an explicit chunk_hits lets the caller pre-resolve (and, on
# failure, degrade) the chunk leg itself instead of unified_search fetching
# it internally — app.api.knowledge_search needs this to catch a chunk
# engine failure without losing the other legs.
# ---------------------------------------------------------------------------


def test_chunk_hits_override_skips_internal_chunk_search():
    from src.search.unified import unified_search

    def _boom(*_a, **_kw):
        raise AssertionError("_chunk_search must not run when chunk_hits is given")

    with (
        patch("src.search.unified._chunk_search", _boom),
        patch("src.search.unified._knowledge_search", lambda q, **kw: []),
        patch("src.search.unified._glossary_search", lambda q, limit=10: []),
    ):
        hits = unified_search(
            "invoices",
            corpus_ids=["c1"],
            user_groups=None,
            granted_domains=None,
            tables=[],
            chunk_hits=[
                {
                    "chunk_id": "ch1",
                    "corpus_id": "c1",
                    "file_id": "f1",
                    "filename": "billing.md",
                    "ordinal": 0,
                    "section_path": None,
                    "text": "invoices are monthly",
                    "score": 0.9,
                    "confidence": "high",
                }
            ],
            k=10,
        )
    assert [h for h in hits if h["type"] == "chunk"]


def test_chunk_hits_empty_list_means_no_chunk_results_not_default_fetch():
    """An explicit empty list (the degraded-leg case) must not fall back to
    fetching internally — `is not None`, not truthiness, is the switch."""
    from src.search.unified import unified_search

    def _boom(*_a, **_kw):
        raise AssertionError("_chunk_search must not run when chunk_hits=[] is given")

    with (
        patch("src.search.unified._chunk_search", _boom),
        patch("src.search.unified._knowledge_search", lambda q, **kw: []),
        patch("src.search.unified._glossary_search", lambda q, limit=10: []),
    ):
        hits = unified_search(
            "invoices",
            corpus_ids=["c1"],
            user_groups=None,
            granted_domains=None,
            tables=[],
            chunk_hits=[],
            k=10,
        )
    assert not [h for h in hits if h["type"] == "chunk"]


def test_chunk_hits_none_preserves_default_internal_fetch():
    """Every existing caller (this file's other ~20 cases) omits
    chunk_hits — the default must be unchanged."""
    from src.search.unified import unified_search

    with (
        patch("src.search.unified._chunk_search", _fake_chunks),
        patch("src.search.unified._knowledge_search", lambda q, **kw: []),
        patch("src.search.unified._glossary_search", lambda q, limit=10: []),
    ):
        hits = unified_search("invoices", corpus_ids=["c1"], user_groups=None, granted_domains=None, tables=[], k=10)
    assert [h for h in hits if h["type"] == "chunk"]
