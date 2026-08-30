"""Fact-graph UI surfaces (eval-critical subset, spec §13.2): collection
detail facts section, Library collection-card counts, and the /admin/access
"nobody" grant_count field.

Backend-parametrized via `seeded_app_both`/`state_backend`: the facts
surface is Postgres-only (A3 ratchet), so every assertion that expects
content to APPEAR skips the DuckDB run — but the "flag off / DuckDB
backend / zero facts -> section simply absent" contract is itself asserted
on BOTH backends, since that is exactly the behavior that must hold
everywhere.
"""

from __future__ import annotations

import re as _re

import pytest


def _admin_headers(s):
    return {"Authorization": f"Bearer {s['admin_token']}"}


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _new_corpus(name: str, slug: str, created_by: str = "admin1") -> str:
    from src.repositories import file_corpora_repo

    return file_corpora_repo().create(name=name, slug=slug, description=None, created_by=created_by)


def _new_file(corpus_id: str, filename: str = "a.md") -> str:
    from src.repositories import corpus_files_repo

    return corpus_files_repo().add(
        corpus_id=corpus_id,
        filename=filename,
        sha256="sha_" + filename,
        file_type="md",
        size_bytes=10,
        storage_path=f"/blobs/{filename}",
    )


def _grant_collection(group_name: str, collection_id: str, member_user_id: str) -> None:
    from src.repositories import resource_grants_repo, user_group_members_repo, user_groups_repo

    grp = user_groups_repo().create(name=group_name, description="test", created_by="admin1")
    user_group_members_repo().add_member(member_user_id, grp["id"], source="admin", added_by="admin1")
    resource_grants_repo().create(grp["id"], "collection", collection_id, "admin1", "available")


# ---------------------------------------------------------------------------
# Collection detail — facts section absence contract (both backends).
# ---------------------------------------------------------------------------


def test_facts_section_absent_when_flag_off(seeded_app_both):
    """Flag off (default): the section simply is not there — no error, no
    empty shell — regardless of backend."""
    s = seeded_app_both
    corpus_id = _new_corpus("No Flag", "no-flag")
    _new_file(corpus_id)
    r = s["client"].get("/library/no-flag", headers=_admin_headers(s))
    assert r.status_code == 200
    assert ">Facts<" not in r.text


def test_facts_section_absent_on_duckdb_even_with_flag_on(seeded_app_both, state_backend, monkeypatch):
    if state_backend != "duckdb":
        pytest.skip("DuckDB-only assertion")
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    s = seeded_app_both
    corpus_id = _new_corpus("Duck Flag", "duck-flag")
    _new_file(corpus_id)
    r = s["client"].get("/library/duck-flag", headers=_admin_headers(s))
    assert r.status_code == 200
    assert ">Facts<" not in r.text


def test_facts_section_absent_when_zero_facts_on_pg(seeded_app_both, state_backend, monkeypatch):
    if state_backend != "pg":
        pytest.skip("PG-only assertion")
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    s = seeded_app_both
    corpus_id = _new_corpus("Zero Facts", "zero-facts")
    _new_file(corpus_id)
    r = s["client"].get("/library/zero-facts", headers=_admin_headers(s))
    assert r.status_code == 200
    assert ">Facts<" not in r.text


# ---------------------------------------------------------------------------
# Collection detail — facts section content + caller scoping (PG only).
# ---------------------------------------------------------------------------


def test_facts_section_present_with_content_on_pg(seeded_app_both, state_backend, monkeypatch):
    if state_backend != "pg":
        pytest.skip("PG-only assertion")
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    s = seeded_app_both
    corpus_id = _new_corpus("Has Facts", "has-facts")
    file_id = _new_file(corpus_id)

    from src.repositories import facts_repo

    fact_id = facts_repo().create_fact(type="engagement")
    facts_repo().add_alias(fact_id=fact_id, type="engagement", natural_key="engagement:acme")
    facts_repo().add_claim(
        fact_id=fact_id,
        corpus_file_id=file_id,
        corpus_id=corpus_id,
        file_sha256="sha_a.md",
        quote="Acme engagement kicked off.",
    )

    r = s["client"].get("/library/has-facts", headers=_admin_headers(s))
    assert r.status_code == 200
    assert ">Facts<" in r.text
    assert "engagement:acme" in r.text
    assert "1 claim" in r.text


def test_facts_section_caller_scoped_two_users_different_grants(seeded_app_both, state_backend, monkeypatch):
    """Two non-admin users with different collection grants see a different
    facts section for the SAME collection — never the collection owner's
    view, always the actual caller's."""
    if state_backend != "pg":
        pytest.skip("PG-only assertion")
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    monkeypatch.setenv("AGNES_FACTS_VISIBILITY_MODE", "all_evidence")
    s = seeded_app_both

    corpus_a = _new_corpus("Corpus A", "corpus-a")
    corpus_b = _new_corpus("Corpus B", "corpus-b")
    file_a = _new_file(corpus_a, "a.md")
    file_b = _new_file(corpus_b, "b.md")

    from src.repositories import facts_repo, users_repo

    fact_id = facts_repo().create_fact(type="engagement")
    facts_repo().add_alias(fact_id=fact_id, type="engagement", natural_key="engagement:spans-both")
    facts_repo().add_claim(
        fact_id=fact_id, corpus_file_id=file_a, corpus_id=corpus_a, file_sha256="sha_a.md", quote="Part A."
    )
    facts_repo().add_claim(
        fact_id=fact_id, corpus_file_id=file_b, corpus_id=corpus_b, file_sha256="sha_b.md", quote="Part B."
    )

    users_repo().create(id="alice1", email="alice1@test.com", name="Alice")
    users_repo().create(id="bob1", email="bob1@test.com", name="Bob")
    alice_token = _issue_token("alice1", "alice1@test.com")
    bob_token = _issue_token("bob1", "bob1@test.com")
    _grant_collection("g-alice-a", corpus_a, "alice1")
    _grant_collection("g-alice-b", corpus_b, "alice1")
    _grant_collection("g-bob-a", corpus_a, "bob1")

    r_alice = s["client"].get("/library/corpus-a", headers=_headers(alice_token))
    r_bob = s["client"].get("/library/corpus-a", headers=_headers(bob_token))
    assert r_alice.status_code == 200
    assert r_bob.status_code == 200
    # Alice can read both A and B -> the fact is visible under all_evidence.
    assert ">Facts<" in r_alice.text
    # Bob can only read A -> under all_evidence the fact is NOT visible.
    assert ">Facts<" not in r_bob.text


def _issue_token(user_id: str, email: str) -> str:
    from app.auth.jwt import create_access_token

    return create_access_token(user_id, email)


def test_facts_section_conflict_rendered_inline(seeded_app_both, state_backend, monkeypatch):
    if state_backend != "pg":
        pytest.skip("PG-only assertion")
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    s = seeded_app_both
    corpus_id = _new_corpus("Conflicting", "conflicting")
    file1 = _new_file(corpus_id, "f1.md")
    file2 = _new_file(corpus_id, "f2.md")

    from src.repositories import facts_repo

    fact_id = facts_repo().create_fact(type="engagement")
    facts_repo().add_claim(
        fact_id=fact_id,
        corpus_file_id=file1,
        corpus_id=corpus_id,
        file_sha256="sha_f1.md",
        quote="Status is active.",
        attrs={"status": "active"},
    )
    facts_repo().add_claim(
        fact_id=fact_id,
        corpus_file_id=file2,
        corpus_id=corpus_id,
        file_sha256="sha_f2.md",
        quote="Status is closed.",
        attrs={"status": "closed"},
    )

    r = s["client"].get("/library/conflicting", headers=_admin_headers(s))
    assert r.status_code == 200
    assert "disagrees across documents" in r.text
    assert "active" in r.text and "closed" in r.text


# ---------------------------------------------------------------------------
# §7.3 — `single_valued_conflict` review items on the collection detail.
# ---------------------------------------------------------------------------


def test_single_valued_conflict_rendered_on_collection_detail(seeded_app_both, state_backend, monkeypatch):
    if state_backend != "pg":
        pytest.skip("PG-only assertion")
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    s = seeded_app_both
    corpus_id = _new_corpus("Owned By", "owned-by")
    file_id = _new_file(corpus_id)

    from src.repositories import facts_repo

    repo = facts_repo()
    src = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=src, type="engagement", natural_key="engagement:acme")
    repo.add_claim(
        fact_id=src, corpus_file_id=file_id, corpus_id=corpus_id, file_sha256="sha_a.md", quote="Acme engagement."
    )
    dst1 = repo.create_fact(type="person")
    repo.add_alias(fact_id=dst1, type="person", natural_key="person:alice")
    repo.add_claim(fact_id=dst1, corpus_file_id=file_id, corpus_id=corpus_id, file_sha256="sha_a.md", quote="Alice.")
    dst2 = repo.create_fact(type="person")
    repo.add_alias(fact_id=dst2, type="person", natural_key="person:bob")
    repo.add_claim(fact_id=dst2, corpus_file_id=file_id, corpus_id=corpus_id, file_sha256="sha_a.md", quote="Bob.")

    edge1 = repo.create_edge(src=src, type="owned_by", dst=dst1)
    repo.add_claim(
        edge_id=edge1, corpus_file_id=file_id, corpus_id=corpus_id, file_sha256="sha_a.md", quote="Owned by Alice."
    )
    edge2 = repo.create_edge(src=src, type="owned_by", dst=dst2)
    repo.add_claim(
        edge_id=edge2, corpus_file_id=file_id, corpus_id=corpus_id, file_sha256="sha_a.md", quote="Owned by Bob."
    )

    r = s["client"].get("/library/owned-by", headers=_admin_headers(s))
    assert r.status_code == 200
    # The review-item marker (not merely the raw names, which would also
    # appear in the ordinary fact list) proves the NEW code path rendered.
    assert 'conflicting "owned_by" values' in r.text
    assert "person:alice" in r.text
    assert "person:bob" in r.text


def test_single_valued_conflict_hidden_when_second_edge_claim_unreadable(seeded_app_both, state_backend, monkeypatch):
    """Same visibility discipline `possible_duplicate_of` review items get
    (spec §7.3): a caller who cannot read the SECOND edge's claim must not
    see the conflict at all — never a one-sided hint of it."""
    if state_backend != "pg":
        pytest.skip("PG-only assertion")
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    s = seeded_app_both

    corpus_a = _new_corpus("Corpus SV A", "corpus-sv-a")
    corpus_b = _new_corpus("Corpus SV B", "corpus-sv-b")
    file_a = _new_file(corpus_a, "a.md")
    file_b = _new_file(corpus_b, "b.md")

    from src.repositories import facts_repo, users_repo

    repo = facts_repo()
    src = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=src, type="engagement", natural_key="engagement:acme-sv")
    repo.add_claim(fact_id=src, corpus_file_id=file_a, corpus_id=corpus_a, file_sha256="sha_a.md", quote="Acme.")

    dst1 = repo.create_fact(type="person")
    repo.add_alias(fact_id=dst1, type="person", natural_key="person:alice-sv")
    repo.add_claim(fact_id=dst1, corpus_file_id=file_a, corpus_id=corpus_a, file_sha256="sha_a.md", quote="Alice.")
    dst2 = repo.create_fact(type="person")
    repo.add_alias(fact_id=dst2, type="person", natural_key="person:bob-sv")
    # Bob's own subject claim ALSO lives only in corpus_b — Ivan (corpus_a
    # only) must not be able to reconstruct the conflict from Bob's fact
    # existing, only from the edge that names him.
    repo.add_claim(fact_id=dst2, corpus_file_id=file_b, corpus_id=corpus_b, file_sha256="sha_b.md", quote="Bob.")

    edge1 = repo.create_edge(src=src, type="owned_by", dst=dst1)
    repo.add_claim(
        edge_id=edge1, corpus_file_id=file_a, corpus_id=corpus_a, file_sha256="sha_a.md", quote="Owned by Alice."
    )
    edge2 = repo.create_edge(src=src, type="owned_by", dst=dst2)
    repo.add_claim(
        edge_id=edge2, corpus_file_id=file_b, corpus_id=corpus_b, file_sha256="sha_b.md", quote="Owned by Bob."
    )

    users_repo().create(id="ivan1", email="ivan1@test.com", name="Ivan")
    ivan_token = _issue_token("ivan1", "ivan1@test.com")
    _grant_collection("g-ivan-sv-a", corpus_a, "ivan1")

    r_ivan = s["client"].get("/library/corpus-sv-a", headers=_headers(ivan_token))
    r_admin = s["client"].get("/library/corpus-sv-a", headers=_admin_headers(s))
    assert r_ivan.status_code == 200
    assert r_admin.status_code == 200
    # Admin sees the full conflict (both dsts) -- Ivan only reaches corpus_a,
    # so edge2's (corpus_b) claim is invisible to him and the conflict drops
    # to a single visible dst, i.e. no conflict at all.
    assert 'conflicting "owned_by" values' in r_admin.text
    assert "person:bob-sv" in r_admin.text
    assert 'conflicting "owned_by" values' not in r_ivan.text
    assert "person:bob-sv" not in r_ivan.text


def test_single_valued_conflict_clears_when_second_dst_claims_deleted(seeded_app_both, state_backend, monkeypatch):
    """Nothing is persisted (spec §7.3): once the second dst's claim is
    gone, the conflict disappears from the very next summary — no admin
    action needed to "resolve" it."""
    if state_backend != "pg":
        pytest.skip("PG-only assertion")
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    s = seeded_app_both
    corpus_id = _new_corpus("Clearing", "clearing-sv")
    file_id = _new_file(corpus_id)

    from src.repositories import facts_repo

    repo = facts_repo()
    src = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=src, type="engagement", natural_key="engagement:clear")
    repo.add_claim(fact_id=src, corpus_file_id=file_id, corpus_id=corpus_id, file_sha256="sha_a.md", quote="Clear.")

    dst1 = repo.create_fact(type="person")
    repo.add_alias(fact_id=dst1, type="person", natural_key="person:one")
    repo.add_claim(fact_id=dst1, corpus_file_id=file_id, corpus_id=corpus_id, file_sha256="sha_a.md", quote="One is.")
    dst2 = repo.create_fact(type="person")
    repo.add_alias(fact_id=dst2, type="person", natural_key="person:two")
    repo.add_claim(fact_id=dst2, corpus_file_id=file_id, corpus_id=corpus_id, file_sha256="sha_a.md", quote="Two is.")

    edge1 = repo.create_edge(src=src, type="owned_by", dst=dst1)
    repo.add_claim(edge_id=edge1, corpus_file_id=file_id, corpus_id=corpus_id, file_sha256="sha_a.md", quote="One.")
    edge2 = repo.create_edge(src=src, type="owned_by", dst=dst2)
    repo.add_claim(edge_id=edge2, corpus_file_id=file_id, corpus_id=corpus_id, file_sha256="sha_a.md", quote="Two.")

    r_before = s["client"].get("/library/clearing-sv", headers=_admin_headers(s))
    # The marker phrase (not the raw "person:two" string, which stays on the
    # page regardless -- dst2's OWN fact claim keeps it in the ordinary fact
    # list even once its edge claim is gone) proves the conflict was live.
    assert 'conflicting "owned_by" values' in r_before.text
    assert "person:two" in r_before.text

    import sqlalchemy as sa
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(sa.text("DELETE FROM claims WHERE edge_id = :eid"), {"eid": edge2})

    r_after = s["client"].get("/library/clearing-sv", headers=_admin_headers(s))
    assert 'conflicting "owned_by" values' not in r_after.text
    # dst2 itself is untouched (still evidenced by its own claim) -- only
    # the review item clears, never the fact.
    assert "person:two" in r_after.text


# ---------------------------------------------------------------------------
# Library collection card — "N files · M facts" (PG only for content).
# ---------------------------------------------------------------------------


def test_library_card_omits_fact_count_when_zero(seeded_app_both, state_backend, monkeypatch):
    """Zero facts on a collection -> today's rendering is unchanged: no
    " · N facts" suffix appended to the card's meta line at all."""
    if state_backend != "pg":
        pytest.skip("PG-only assertion")
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    s = seeded_app_both
    _new_corpus("No Facts Card", "no-facts-card")
    r = s["client"].get("/library", headers=_admin_headers(s))
    assert r.status_code == 200
    # "fact" alone is a substring of unrelated nav copy ("Artefacts") — the
    # meta-line suffix this surface adds always pairs a digit with the word.
    assert not _re.search(r"\d+\s+facts?\b", r.text)


def test_library_card_shows_fact_count_caller_scoped(seeded_app_both, state_backend, monkeypatch):
    if state_backend != "pg":
        pytest.skip("PG-only assertion")
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    s = seeded_app_both

    corpus_id = _new_corpus("Card Facts", "card-facts", created_by="owner1")
    corpus_id2 = _new_corpus("Card Facts Second File", "card-facts-2", created_by="owner1")
    del corpus_id2
    file_id = _new_file(corpus_id)

    from src.repositories import facts_repo, users_repo

    fact_id = facts_repo().create_fact(type="engagement")
    facts_repo().add_claim(
        fact_id=fact_id, corpus_file_id=file_id, corpus_id=corpus_id, file_sha256="sha_a.md", quote="Evidence."
    )

    users_repo().create(id="owner1", email="owner1@test.com", name="Owner")
    users_repo().create(id="carol1", email="carol1@test.com", name="Carol")
    carol_token = _issue_token("carol1", "carol1@test.com")
    _grant_collection("g-carol-card", corpus_id, "carol1")

    r = s["client"].get("/library", headers=_headers(carol_token))
    assert r.status_code == 200
    assert "1 fact" in r.text


def test_library_card_fact_counts_do_not_grow_with_the_number_of_collections(
    seeded_app_both, state_backend, monkeypatch
):
    """The Library renders one card per collection, and the singular count
    re-resolved the caller's readable set every time — `accessible_collection_ids`
    runs a grants query plus a full owned-collections scan, so the page cost
    grew with the number of collections a caller can see. The batch call
    resolves it once no matter how many cards render (Devin Review on #1652).

    Asserts that invariant directly — the call count is IDENTICAL for four
    collections and for eight — rather than pinning a magic total. The page
    legitimately has more than one page-level consumer of the readable set
    (the card counts here, and the Knowledge tab's type map via
    ``count_visible_facts_by_type``), and each resolving once is fine; what
    must never come back is a resolution per CARD, which this catches
    whatever the constant happens to be.
    """
    if state_backend != "pg":
        pytest.skip("PG-only assertion")
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    s = seeded_app_both

    from src.repositories import facts_repo, users_repo

    users_repo().create(id="owner9", email="owner9@test.com", name="Owner9")
    users_repo().create(id="erin1", email="erin1@test.com", name="Erin")
    erin_token = _issue_token("erin1", "erin1@test.com")

    import app.auth.access as access

    calls = {"n": 0}
    real = access.accessible_collection_ids

    def _counting(caller):
        calls["n"] += 1
        return real(caller)

    monkeypatch.setattr(access, "accessible_collection_ids", _counting)

    def _add_collections(lo, hi):
        for i in range(lo, hi):
            cid = _new_corpus(f"Batch {i}", f"batch-{i}", created_by="owner9")
            fid = _new_file(cid, f"b{i}.md")
            fact_id = facts_repo().create_fact(type="engagement")
            facts_repo().add_claim(
                fact_id=fact_id, corpus_file_id=fid, corpus_id=cid, file_sha256=f"sha_b{i}.md", quote=f"E{i}."
            )
            _grant_collection(f"g-erin-{i}", cid, "erin1")

    def _render():
        calls["n"] = 0
        r = s["client"].get("/library", headers=_headers(erin_token))
        assert r.status_code == 200
        return r, calls["n"]

    _add_collections(0, 4)
    r4, n4 = _render()
    assert r4.text.count("1 fact") >= 4, "all four cards must still carry their own count"

    _add_collections(4, 8)
    r8, n8 = _render()
    assert r8.text.count("1 fact") >= 8, "all eight cards must still carry their own count"

    assert n8 == n4, (
        "the readable set must be resolved a fixed number of times per page, "
        f"not once per card (4 collections -> {n4} resolutions, 8 -> {n8})"
    )

def test_library_card_fact_count_zero_for_ungranted_caller(seeded_app_both, state_backend, monkeypatch):
    """A caller who cannot reach a collection at all never sees its row —
    but the mechanism is still caller-scoped, not owner-scoped: proven by
    the granted-caller test above returning a DIFFERENT number than an
    admin god-mode view would if the fact spanned an unreadable corpus."""
    if state_backend != "pg":
        pytest.skip("PG-only assertion")
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    monkeypatch.setenv("AGNES_FACTS_VISIBILITY_MODE", "all_evidence")
    s = seeded_app_both

    corpus_a = _new_corpus("Split A", "split-a", created_by="owner2")
    corpus_b = _new_corpus("Split B", "split-b", created_by="owner2")
    file_a = _new_file(corpus_a, "sa.md")
    file_b = _new_file(corpus_b, "sb.md")

    from src.repositories import facts_repo, users_repo

    fact_id = facts_repo().create_fact(type="engagement")
    facts_repo().add_claim(
        fact_id=fact_id, corpus_file_id=file_a, corpus_id=corpus_a, file_sha256="sha_sa.md", quote="A."
    )
    facts_repo().add_claim(
        fact_id=fact_id, corpus_file_id=file_b, corpus_id=corpus_b, file_sha256="sha_sb.md", quote="B."
    )

    users_repo().create(id="owner2", email="owner2@test.com", name="Owner2")
    users_repo().create(id="dana1", email="dana1@test.com", name="Dana")
    users_repo().create(id="erin3", email="erin3@test.com", name="Erin3")
    dana_token = _issue_token("dana1", "dana1@test.com")
    erin_token = _issue_token("erin3", "erin3@test.com")
    _grant_collection("g-dana-both-a", corpus_a, "dana1")
    _grant_collection("g-dana-both-b", corpus_b, "dana1")
    _grant_collection("g-erin-a-only", corpus_a, "erin3")

    r_dana = s["client"].get("/library/split-a", headers=_headers(dana_token))
    r_erin = s["client"].get("/library/split-a", headers=_headers(erin_token))
    assert r_dana.status_code == 200
    assert r_erin.status_code == 200
    assert ">Facts<" in r_dana.text
    assert ">Facts<" not in r_erin.text


# ---------------------------------------------------------------------------
# /admin/access — the "⚠ nobody" grant_count field (both backends: the
# collection resource type itself is backend-agnostic — see
# tests/test_resource_types.py for the DuckDB-side unit coverage).
# ---------------------------------------------------------------------------


def test_access_overview_grant_count_zero_for_ungranted_collection(seeded_app_both):
    s = seeded_app_both
    _new_corpus("Nobody", "nobody-col")
    r = s["client"].get("/api/admin/access-overview", headers=_admin_headers(s))
    assert r.status_code == 200
    body = r.json()
    collection_type = next(t for t in body["resources"] if t["type_key"] == "collection")
    items = [i for b in collection_type["blocks"] for i in b["items"]]
    row = next(i for i in items if i["name"] == "Nobody")
    assert row["grant_count"] == 0


def test_access_overview_grant_count_reflects_grants(seeded_app_both):
    s = seeded_app_both
    corpus_id = _new_corpus("Granted", "granted-col")
    _grant_collection("g-access-overview", corpus_id, "admin1")

    r = s["client"].get("/api/admin/access-overview", headers=_admin_headers(s))
    assert r.status_code == 200
    body = r.json()
    collection_type = next(t for t in body["resources"] if t["type_key"] == "collection")
    items = [i for b in collection_type["blocks"] for i in b["items"]]
    row = next(i for i in items if i["resource_id"] == corpus_id)
    assert row["grant_count"] == 1
