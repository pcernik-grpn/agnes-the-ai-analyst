"""Relationship-shaped read surface for the fact graph (TCRD-295):
``edges()``, bounded inline claims on ``edges()``/``neighbors()``/``search()``,
the ``claims()`` row cap, and the batched per-hop endpoint check in
``neighbors()``.

Why this exists (measured on the Cuesta instance, 2026-09-02): a one-hop
relationship question cost 8 tool calls and 2-4 minutes, a G1-shaped
question 252k tokens in one turn, because the five primitives are shaped by
the security model (what can I see) rather than by the questions (how does X
relate to Y) — every relationship is one ``fact_neighbors`` per root plus one
``fact_claims`` per citation, and ``fact_claims`` has no cap (one probe
returned 18.5k tokens that every later step re-read).

Same discipline as ``test_facts_read_pg.py`` (this module reuses its
fixtures): every visibility assertion proves itself through a NON-admin
caller with a deliberately scoped grant; fixtures are uploaded by an account
that is never the probed caller; the S-rules from spec §5 hold on the new
surface exactly as on the old one — filter in SQL before LIMIT, no shortfall
oracle, no tunnelling through an invisible endpoint.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tests.db_pg.test_facts_read_pg import (  # noqa: F401  (pg_env/repo are fixtures)
    CORPUS_A,
    CORPUS_B,
    _agent_principal,
    _dict_user,
    _make_group_with_grant,
    _seed_collection,
    _seed_corpus_file,
    _seed_uploader,
    pg_env,
    repo,
)

D = dt.date


# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------


def _reader(pg_env, user_id: str, *collection_ids: str) -> dict:
    """A plain dict user granted exactly ``collection_ids`` through one
    group per collection (never the uploader)."""
    from src.repositories import users_repo

    users_repo().create(id=user_id, email=f"{user_id}@test.com", name=user_id)
    for i, cid in enumerate(collection_ids):
        _make_group_with_grant(pg_env, group_name=f"grp-{user_id}-{i}", collection_id=cid, member_user_id=user_id)
    return _dict_user(user_id)


def _claim(
    repo, *, fact_id=None, edge_id=None, corpus=CORPUS_A, file_id="cf_a1", quote, attrs=None, date=None, audience=None
):
    return repo.add_claim(
        fact_id=fact_id,
        edge_id=edge_id,
        corpus_file_id=file_id,
        corpus_id=corpus,
        file_sha256="sha1",
        quote=quote,
        attrs=attrs,
        document_date=date,
        audience=audience,
    )


def _seed_two_collections():
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a2")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")


def _seed_g1_graph(repo):
    """A G1-shaped graph, generic vocabulary (no customer names): one
    ``sponsor`` owns two ``client``s (client -owned_by-> sponsor), each
    client sits in one ``industry`` (client -in_industry-> industry), plus
    one unrelated ``knows`` edge so a type filter has something to exclude.
    Every claim readable from CORPUS_A."""
    ids = {}

    def _named(fact_type: str, slug: str) -> str:
        fid = repo.create_fact(type=fact_type)
        key = f"{fact_type}:{slug}"
        repo.add_alias(fact_id=fid, type=fact_type, natural_key=key)
        # S9: a non-admin sees an alias only with readable provenance.
        repo.add_alias_source(type=fact_type, natural_key=key, corpus_id=CORPUS_A)
        return fid

    ids["sponsor"] = _named("sponsor", "acme-capital")
    _claim(repo, fact_id=ids["sponsor"], quote="Acme Capital is a sponsor.")
    for n in ("c1", "c2"):
        ids[n] = _named("client", n)
        _claim(repo, fact_id=ids[n], quote=f"{n} is a client.", attrs={"status": "active"})
    for n in ("i1", "i2"):
        ids[n] = _named("industry", n)
        _claim(repo, fact_id=ids[n], quote=f"{n} is an industry.")
    ids["e_c1_sponsor"] = repo.create_edge(src=ids["c1"], type="owned_by", dst=ids["sponsor"])
    _claim(repo, edge_id=ids["e_c1_sponsor"], quote="c1 is owned by Acme Capital.", attrs={"since": 2021})
    ids["e_c2_sponsor"] = repo.create_edge(src=ids["c2"], type="owned_by", dst=ids["sponsor"])
    _claim(repo, edge_id=ids["e_c2_sponsor"], quote="c2 is owned by Acme Capital.", attrs={"since": 2023})
    ids["e_c1_i1"] = repo.create_edge(src=ids["c1"], type="in_industry", dst=ids["i1"])
    _claim(repo, edge_id=ids["e_c1_i1"], quote="c1 operates in i1.")
    ids["e_c2_i2"] = repo.create_edge(src=ids["c2"], type="in_industry", dst=ids["i2"])
    _claim(repo, edge_id=ids["e_c2_i2"], quote="c2 operates in i2.")
    ids["e_knows"] = repo.create_edge(src=ids["c1"], type="knows", dst=ids["c2"])
    _claim(repo, edge_id=ids["e_knows"], quote="c1 knows c2.")
    return ids


# ---------------------------------------------------------------------------
# edges() — one relationship-shaped read
# ---------------------------------------------------------------------------


def test_edges_lists_visible_edges_of_one_type_with_both_endpoints_projected(pg_env, repo):
    """The G1 shape in ONE call: every readable ``owned_by`` edge, with both
    endpoints carried as full subjects (same shape as ``neighbors()`` nodes)
    and the edge's own projected attrs — no per-root ``neighbors`` fan-out."""
    _seed_two_collections()
    ids = _seed_g1_graph(repo)
    rex = _reader(pg_env, "rex", CORPUS_A)

    result = repo.edges(rex, edge_type="owned_by")

    assert {e["id"] for e in result["edges"]} == {ids["e_c1_sponsor"], ids["e_c2_sponsor"]}
    by_id = {e["id"]: e for e in result["edges"]}
    assert by_id[ids["e_c1_sponsor"]]["src"] == ids["c1"]
    assert by_id[ids["e_c1_sponsor"]]["dst"] == ids["sponsor"]
    assert by_id[ids["e_c1_sponsor"]]["type"] == "owned_by"
    assert by_id[ids["e_c1_sponsor"]]["attrs"]["since"]["value"] == 2021

    nodes = {n["id"]: n for n in result["nodes"]}
    assert set(nodes) == {ids["c1"], ids["c2"], ids["sponsor"]}
    sponsor = nodes[ids["sponsor"]]
    assert sponsor["type"] == "sponsor"
    assert sponsor["aliases"] == ["sponsor:acme-capital"]
    assert sponsor["claim_count"] == 1
    assert sponsor["quote_count"] == 1
    assert sponsor["revealed"] is False
    assert nodes[ids["c1"]]["attrs"]["status"]["value"] == "active"
    assert result["truncated"] == {"result": False, "extension": False, "claims": False}


def test_edges_unknown_type_is_an_empty_result_not_an_error(pg_env, repo):
    """Absence and non-existence must read the same (spec §5 rule 2)."""
    _seed_two_collections()
    _seed_g1_graph(repo)
    rex = _reader(pg_env, "rex", CORPUS_A)

    result = repo.edges(rex, edge_type="no_such_relationship")
    assert result["edges"] == [] and result["nodes"] == []


def test_edges_s3_edge_with_only_unreadable_claim_is_never_returned(pg_env, repo):
    """S3 on the new surface: an edge evidenced only in an unreadable
    collection is not listed, and its far endpoint is not carried along."""
    _seed_two_collections()
    ids = _seed_g1_graph(repo)
    hidden_client = repo.create_fact(type="client")
    _claim(repo, fact_id=hidden_client, quote="hidden client exists.")
    hidden_edge = repo.create_edge(src=hidden_client, type="owned_by", dst=ids["sponsor"])
    _claim(repo, edge_id=hidden_edge, corpus=CORPUS_B, file_id="cf_b1", quote="hidden owned by Acme.")
    alice = _reader(pg_env, "alice", CORPUS_A)

    result = repo.edges(alice, edge_type="owned_by")
    assert hidden_edge not in {e["id"] for e in result["edges"]}
    assert hidden_client not in {n["id"] for n in result["nodes"]}


def test_edges_excludes_an_edge_into_a_withheld_endpoint(pg_env, repo):
    """Mirrors ``neighbors()``'s rule 3: an edge whose endpoint is under a
    ``restricted`` correction is not listed even though the edge's own
    claim is readable — never reveal that the relationship continues into
    something the caller may not see."""
    _seed_two_collections()
    ids = _seed_g1_graph(repo)
    repo.upsert_correction(
        subject_kind="fact", subject_id=ids["c2"], natural_keys=[], verdict="restricted", reason="t", decided_by="t"
    )
    rex = _reader(pg_env, "rex", CORPUS_A)

    result = repo.edges(rex, edge_type="owned_by")
    assert {e["id"] for e in result["edges"]} == {ids["e_c1_sponsor"]}
    assert ids["c2"] not in {n["id"] for n in result["nodes"]}


def test_edges_endpoint_type_and_id_filters_narrow_in_sql(pg_env, repo):
    _seed_two_collections()
    ids = _seed_g1_graph(repo)
    rex = _reader(pg_env, "rex", CORPUS_A)

    only_c2 = repo.edges(rex, edge_type="owned_by", src_id=ids["c2"])
    assert [e["id"] for e in only_c2["edges"]] == [ids["e_c2_sponsor"]]

    by_dst_type = repo.edges(rex, edge_type="owned_by", dst_type="industry")
    assert by_dst_type["edges"] == []

    by_src_type = repo.edges(rex, edge_type="in_industry", src_type="client", dst_id=ids["i1"])
    assert [e["id"] for e in by_src_type["edges"]] == [ids["e_c1_i1"]]


def test_edges_truncation_flag_is_the_callers_own_shortfall_never_a_grant_oracle(pg_env, repo):
    """S6: ``truncated.result`` is true only when the caller's OWN visible
    set exceeds ``limit`` — an unreadable edge beyond the page must not flip
    it (a short page never signals hidden matches)."""
    _seed_two_collections()
    ids = _seed_g1_graph(repo)
    # a third owned_by edge readable only in CORPUS_B
    c3 = repo.create_fact(type="client")
    _claim(repo, fact_id=c3, quote="c3 exists.")
    e3 = repo.create_edge(src=c3, type="owned_by", dst=ids["sponsor"])
    _claim(repo, edge_id=e3, corpus=CORPUS_B, file_id="cf_b1", quote="c3 owned by Acme.")
    alice = _reader(pg_env, "alice", CORPUS_A)

    short = repo.edges(alice, edge_type="owned_by", limit=1)
    assert len(short["edges"]) == 1 and short["truncated"]["result"] is True

    exact = repo.edges(alice, edge_type="owned_by", limit=2)
    assert len(exact["edges"]) == 2 and exact["truncated"]["result"] is False


def test_edges_extension_hop_follows_a_second_edge_type_from_the_chosen_endpoint(pg_env, repo):
    """G1's second hop in the same call: from every ``src`` of the primary
    edges (the clients), follow ``in_industry`` — the extension is
    visibility-gated exactly like the primary set, and an unreadable
    extension edge is neither listed nor flagged."""
    _seed_two_collections()
    ids = _seed_g1_graph(repo)
    # an unreadable in_industry edge off c1
    i3 = repo.create_fact(type="industry")
    _claim(repo, fact_id=i3, quote="i3 exists.")
    e_hidden = repo.create_edge(src=ids["c1"], type="in_industry", dst=i3)
    _claim(repo, edge_id=e_hidden, corpus=CORPUS_B, file_id="cf_b1", quote="c1 in i3.")
    alice = _reader(pg_env, "alice", CORPUS_A)

    result = repo.edges(alice, edge_type="owned_by", extend_edge_type="in_industry", extend_from="src")

    edge_ids = {e["id"] for e in result["edges"]}
    assert edge_ids == {ids["e_c1_sponsor"], ids["e_c2_sponsor"], ids["e_c1_i1"], ids["e_c2_i2"]}
    assert e_hidden not in edge_ids
    node_ids = {n["id"] for n in result["nodes"]}
    assert node_ids == {ids["sponsor"], ids["c1"], ids["c2"], ids["i1"], ids["i2"]}
    assert i3 not in node_ids
    assert result["truncated"]["extension"] is False


def test_edges_extension_is_capped_and_flagged_independently(pg_env, repo):
    _seed_two_collections()
    ids = _seed_g1_graph(repo)
    rex = _reader(pg_env, "rex", CORPUS_A)

    result = repo.edges(rex, edge_type="owned_by", extend_edge_type="in_industry", extend_from="src", limit=1)
    # primary capped at 1 (of 2) -> result truncated; extension from that one
    # client yields exactly one in_industry edge -> not truncated.
    assert result["truncated"]["result"] is True
    assert result["truncated"]["extension"] is False
    assert len([e for e in result["edges"] if e["type"] == "in_industry"]) == 1


def test_edges_s5_restricted_agent_principal_sees_its_scoped_subset(pg_env, repo):
    _seed_two_collections()
    ids = _seed_g1_graph(repo)
    c3 = repo.create_fact(type="client")
    _claim(repo, fact_id=c3, corpus=CORPUS_B, file_id="cf_b1", quote="c3 exists.")
    e3 = repo.create_edge(src=c3, type="owned_by", dst=ids["sponsor"])
    _claim(repo, edge_id=e3, corpus=CORPUS_B, file_id="cf_b1", quote="c3 owned by Acme.")

    scoped = repo.edges(_agent_principal([CORPUS_A]), edge_type="owned_by")
    assert {e["id"] for e in scoped["edges"]} == {ids["e_c1_sponsor"], ids["e_c2_sponsor"]}
    wide = repo.edges(_agent_principal([CORPUS_A, CORPUS_B]), edge_type="owned_by")
    assert e3 in {e["id"] for e in wide["edges"]}


def test_edges_applies_a_statement_timeout(pg_env, repo):
    from sqlalchemy import event

    _seed_two_collections()
    _seed_g1_graph(repo)
    rex = _reader(pg_env, "rex", CORPUS_A)
    statements: list = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(repo._engine, "before_cursor_execute", _capture)
    try:
        repo.edges(rex, edge_type="owned_by")
    finally:
        event.remove(repo._engine, "before_cursor_execute", _capture)
    assert any("SET LOCAL statement_timeout" in s for s in statements)


# ---------------------------------------------------------------------------
# bounded inline claims — k newest readable claims per subject, after dedup,
# inheriting claims()'s revealed/opaque-document rules.
# ---------------------------------------------------------------------------


def test_edges_include_claims_attaches_the_k_newest_readable_claims_per_edge(pg_env, repo):
    _seed_two_collections()
    ids = _seed_g1_graph(repo)
    e = ids["e_c1_sponsor"]  # already carries one undated claim
    _claim(repo, edge_id=e, file_id="cf_a2", quote="Oldest dated.", date=D(2020, 1, 1))
    _claim(repo, edge_id=e, quote="Newest dated.", date=D(2024, 6, 1))
    _claim(repo, edge_id=e, file_id="cf_a2", quote="Middle dated.", date=D(2022, 3, 3))
    rex = _reader(pg_env, "rex", CORPUS_A)

    result = repo.edges(rex, edge_type="owned_by", src_id=ids["c1"], include_claims=2)
    edge = result["edges"][0]
    assert [c["quote"] for c in edge["claims"]] == ["Newest dated.", "Middle dated."]
    assert edge["claims"][0]["document"]["name"] == "cf_a1.md"
    assert edge["claims"][0]["document_date"] == "2024-06-01"
    # inline claims ride on the relationship (the thing that gets cited);
    # nodes keep their claim_count and are fetched via fact_claims on demand.
    assert all("claims" not in n for n in result["nodes"])


def test_edges_include_claims_zero_attaches_nothing(pg_env, repo):
    _seed_two_collections()
    _seed_g1_graph(repo)
    rex = _reader(pg_env, "rex", CORPUS_A)
    result = repo.edges(rex, edge_type="owned_by")
    assert all("claims" not in e for e in result["edges"])


def test_edges_include_claims_never_carries_an_unreadable_claim(pg_env, repo):
    """The inline path runs through the SAME ``vis`` gate as ``claims()`` —
    a claim in an unreadable collection is not among the k, even when it
    is the newest."""
    _seed_two_collections()
    ids = _seed_g1_graph(repo)
    e = ids["e_c1_sponsor"]
    _claim(repo, edge_id=e, corpus=CORPUS_B, file_id="cf_b1", quote="Secret newest.", date=D(2030, 1, 1))
    alice = _reader(pg_env, "alice", CORPUS_A)

    result = repo.edges(alice, edge_type="owned_by", src_id=ids["c1"], include_claims=3)
    quotes = [c["quote"] for c in result["edges"][0]["claims"]]
    assert "Secret newest." not in quotes
    assert quotes == ["c1 is owned by Acme Capital."]


def test_edges_include_claims_counts_k_after_audience_dedup(pg_env, repo, monkeypatch):
    """Zdeněk's review condition: k is computed AFTER audience-variant dedup
    and inherits ``claims()``'s rules — a ``full``/``redacted`` pair on one
    file is ONE claim (the most privileged variant), never two rows eating
    the budget, and the ``redacted`` text never reaches a ``full`` reader."""
    from tests.db_pg.test_facts_audience import _add_member, _seed_matrix

    monkeypatch.delenv("AGNES_ACL_GUARANTEE_MODE", raising=False)
    full_gid, reader_gid, fact_plain, fact_untagged, fact_mixed = _seed_matrix(pg_env, repo)
    # an edge between the two visible facts, evidenced by a tiered pair on
    # cf_mixed (newest) and one untagged claim on cf_plain (older)
    edge_id = repo.create_edge(src=fact_plain, type="related_to", dst=fact_mixed)
    repo.add_claim(
        edge_id=edge_id,
        corpus_file_id="cf_mixed",
        corpus_id="col_tiered",
        file_sha256="sha1",
        quote="Deal size $20k.",
        audience="full",
        document_date=D(2025, 1, 1),
    )
    repo.add_claim(
        edge_id=edge_id,
        corpus_file_id="cf_mixed",
        corpus_id="col_tiered",
        file_sha256="sha1",
        quote="Deal size <redacted>.",
        audience="redacted",
        document_date=D(2025, 1, 1),
    )
    repo.add_claim(
        edge_id=edge_id,
        corpus_file_id="cf_plain",
        corpus_id="col_plain",
        file_sha256="sha1",
        quote="Plain older evidence.",
        document_date=D(2020, 1, 1),
    )
    from src.repositories import users_repo

    users_repo().create(id="topclass", email="topclass@test.com", name="TopClass")
    _add_member("topclass", reader_gid)
    _add_member("topclass", full_gid)

    result = repo.edges(_dict_user("topclass"), edge_type="related_to", include_claims=2)
    quotes = [c["quote"] for c in result["edges"][0]["claims"]]
    assert quotes == ["Deal size $20k.", "Plain older evidence."]


def test_edges_include_claims_on_a_revealed_edge_withholds_quotes_and_opaque_documents(pg_env, repo):
    """A ``revealed`` edge is listed for everyone; its inline claims follow
    ``claims()``: every quote blank, and the document identity withheld
    (``None``) for a claim whose collection the caller cannot read."""
    _seed_two_collections()
    ids = _seed_g1_graph(repo)
    e = repo.create_edge(src=ids["c1"], type="advised_by", dst=ids["sponsor"])
    _claim(repo, edge_id=e, corpus=CORPUS_B, file_id="cf_b1", quote="Secret advisory.")
    repo.upsert_correction(
        subject_kind="edge", subject_id=e, natural_keys=[], verdict="revealed", reason="t", decided_by="t"
    )
    alice = _reader(pg_env, "alice", CORPUS_A)

    result = repo.edges(alice, edge_type="advised_by", include_claims=1)
    assert [x["id"] for x in result["edges"]] == [e]
    claim = result["edges"][0]["claims"][0]
    assert claim["quote"] == ""
    assert claim["document"] is None


def test_edges_include_claims_truncates_long_quotes_and_says_so(pg_env, repo):
    from src.repositories import facts_pg

    _seed_two_collections()
    ids = _seed_g1_graph(repo)
    long_quote = "x" * (facts_pg.INLINE_QUOTE_MAX_CHARS + 50)
    _claim(repo, edge_id=ids["e_c2_sponsor"], quote=long_quote, date=D(2025, 1, 1))
    rex = _reader(pg_env, "rex", CORPUS_A)

    result = repo.edges(rex, edge_type="owned_by", src_id=ids["c2"], include_claims=1)
    claim = result["edges"][0]["claims"][0]
    assert len(claim["quote"]) == facts_pg.INLINE_QUOTE_MAX_CHARS
    assert claim["quote_truncated"] is True


def test_edges_include_claims_respects_the_total_budget_and_flags_it(pg_env, repo, monkeypatch):
    from src.repositories import facts_pg

    monkeypatch.setattr(facts_pg, "MAX_INLINE_CLAIMS_TOTAL", 1)
    _seed_two_collections()
    _seed_g1_graph(repo)
    rex = _reader(pg_env, "rex", CORPUS_A)

    result = repo.edges(rex, edge_type="owned_by", include_claims=1)
    attached = sum(len(e.get("claims", [])) for e in result["edges"])
    assert attached == 1
    assert result["truncated"]["claims"] is True


def test_neighbors_include_claims_attaches_claims_to_edges(pg_env, repo):
    _seed_two_collections()
    ids = _seed_g1_graph(repo)
    rex = _reader(pg_env, "rex", CORPUS_A)

    result = repo.neighbors(rex, ids["sponsor"], edge_types=["owned_by"], include_claims=1)
    edges = {e["id"]: e for e in result["edges"]}
    assert edges[ids["e_c1_sponsor"]]["claims"][0]["quote"] == "c1 is owned by Acme Capital."
    assert result["truncated"]["claims"] is False


def test_neighbors_without_include_claims_is_byte_identical_to_before(pg_env, repo):
    _seed_two_collections()
    ids = _seed_g1_graph(repo)
    rex = _reader(pg_env, "rex", CORPUS_A)
    result = repo.neighbors(rex, ids["sponsor"])
    assert all("claims" not in e for e in result["edges"])
    assert all("claims" not in n for n in result["nodes"])
    assert set(result["truncated"]) == {"depth", "fanout", "result"}


def test_search_include_claims_attaches_claims_to_subjects(pg_env, repo):
    _seed_two_collections()
    ids = _seed_g1_graph(repo)
    rex = _reader(pg_env, "rex", CORPUS_A)

    result = repo.search(rex, type="sponsor", include_claims=1)
    assert result["subjects"][0]["id"] == ids["sponsor"]
    assert result["subjects"][0]["claims"][0]["quote"] == "Acme Capital is a sponsor."
    assert result["claims_truncated"] is False


# ---------------------------------------------------------------------------
# claims() cap — newest first, with the caller's-own-shortfall signal
# ---------------------------------------------------------------------------


def test_claims_caps_at_limit_newest_first_and_signals_the_callers_own_shortfall(pg_env, repo):
    _seed_two_collections()
    fact = repo.create_fact(type="person")
    for i, d in enumerate([D(2021, 1, 1), D(2023, 1, 1), D(2022, 1, 1), None, D(2024, 1, 1)]):
        _claim(repo, fact_id=fact, quote=f"q{i}", date=d, file_id="cf_a1" if i % 2 else "cf_a2")
    rex = _reader(pg_env, "rex", CORPUS_A)

    result = repo.claims(rex, fact, limit=3)
    assert [c["quote"] for c in result["claims"]] == ["q4", "q1", "q2"]
    assert result["limit_applied"] is True

    full = repo.claims(rex, fact, limit=10)
    assert [c["quote"] for c in full["claims"]] == ["q4", "q1", "q2", "q0", "q3"]
    assert full["limit_applied"] is False


def test_claims_limit_applied_never_counts_unreadable_claims(pg_env, repo):
    """S6 on the capped ``claims()``: five unreadable claims beyond the
    page must not flip ``limit_applied``."""
    _seed_two_collections()
    fact = repo.create_fact(type="person")
    _claim(repo, fact_id=fact, quote="readable 1")
    _claim(repo, fact_id=fact, quote="readable 2")
    for i in range(5):
        _claim(repo, fact_id=fact, corpus=CORPUS_B, file_id="cf_b1", quote=f"secret {i}")
    alice = _reader(pg_env, "alice", CORPUS_A)

    result = repo.claims(alice, fact, limit=3)
    assert len(result["claims"]) == 2
    assert result["limit_applied"] is False


def test_claims_limit_is_clamped_to_the_repository_ceiling(pg_env, repo):
    from src.repositories import facts_pg

    _seed_two_collections()
    fact = repo.create_fact(type="person")
    for i in range(facts_pg.MAX_CLAIMS_LIMIT + 2):
        _claim(repo, fact_id=fact, quote=f"q{i}")
    rex = _reader(pg_env, "rex", CORPUS_A)

    result = repo.claims(rex, fact, limit=10_000)
    assert len(result["claims"]) == facts_pg.MAX_CLAIMS_LIMIT
    assert result["limit_applied"] is True


# ---------------------------------------------------------------------------
# neighbors() — one endpoint-status query per hop, not two per node
# ---------------------------------------------------------------------------


def test_neighbors_checks_endpoint_visibility_once_per_hop_not_per_node(pg_env, repo):
    """The N+1: today every discovered node costs a ``_subject_status``
    query plus a ``SELECT id, type FROM facts`` — five neighbors is eleven
    round trips. After batching: the root's own status check plus ONE
    batched status query and ONE type lookup per hop."""
    from sqlalchemy import event

    _seed_two_collections()
    hub = repo.create_fact(type="person")
    _claim(repo, fact_id=hub, quote="hub exists.")
    for i in range(5):
        other = repo.create_fact(type="person")
        _claim(repo, fact_id=other, quote=f"other {i} exists.")
        e = repo.create_edge(src=hub, type="knows", dst=other)
        _claim(repo, edge_id=e, quote=f"hub knows other {i}.")
    rex = _reader(pg_env, "rex", CORPUS_A)
    statements: list = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(repo._engine, "before_cursor_execute", _capture)
    try:
        result = repo.neighbors(rex, hub, depth=1)
    finally:
        event.remove(repo._engine, "before_cursor_execute", _capture)

    assert len(result["nodes"]) == 6 and len(result["edges"]) == 5
    status_queries = [s for s in statements if "relevant_claims" in s]
    assert len(status_queries) == 2, f"expected root + one batched hop, got {len(status_queries)}"
