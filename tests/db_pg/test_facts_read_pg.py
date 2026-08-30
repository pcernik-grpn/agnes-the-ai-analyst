"""Read-path RBAC tests for the fact graph over Collections (build order
steps 2+3 of
docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md).

PG-only, no DuckDB half to parametrize against (A3 ratchet) — see
``docs/migrations.md`` -> "Adding a PG-only feature". Fixtures are seeded
directly through :class:`FactsPgRepository`'s write/seed methods (never via
ingest — that lands in the write-path follow-up task).

Every S-id below is the literal acceptance test named in spec §15.1;
docstrings restate the failure mode.

Every visibility/filtering assertion here proves itself through a non-admin
caller (a plain dict user with a deliberately withheld or scoped grant, or a
restricted ``AgentPrincipal``) — never through the ``Admin`` god-mode
short-circuit alone. An admin-sees-everything case is a legitimate, separate
sibling assertion (e.g. ``test_count_visible_edges_for_collections_admin_
sees_everything``), not a substitute for the caller-scoped one — see
CONTRIBUTING.md's "Testing conventions" for why (this module is the reason
that section exists).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_A = "col_a"
CORPUS_B = "col_b"


# ---------------------------------------------------------------------------
# fixtures / seeding helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_env(tmp_path, monkeypatch, pg_engine):
    """Alembic-upgraded Postgres wired as the active backend for the repo
    factory + ``app.auth.access``'s group/grant primitives (mirrors
    ``tests/db_pg/test_resolve_agent_authority_pg.py``)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from tests.db_pg._parity_sweep_util import _seed_pg_system_groups

    _seed_pg_system_groups(pg_engine)
    return pg_engine


@pytest.fixture
def repo(pg_env):
    from src.repositories.facts_pg import FactsPgRepository

    import src.db_pg as db_pg

    return FactsPgRepository(db_pg.get_engine())


def _dict_user(user_id: str) -> dict:
    return {"id": user_id, "email": f"{user_id}@test.com"}


def _agent_principal(collection_ids):
    """A restricted AgentPrincipal scoped to exactly ``collection_ids`` —
    constructed the way the agent-authority code does (frozen dataclass,
    live intersection, no owner identity to fall back on)."""
    from app.auth.session_principal import AgentPrincipal

    return AgentPrincipal(
        session_id="sess1",
        agent_id="agent1",
        owner_user_id="owner-not-the-caller",
        owner_email="owner@test.com",
        intersection={"collection": frozenset(collection_ids)},
    )


def _make_group_with_grant(pg_engine, *, group_name: str, collection_id: str, member_user_id: str) -> None:
    """Seed a group holding a COLLECTION grant, with ``member_user_id`` as
    its sole member. The caller must already exist as a ``users`` row
    (created by ``_seed_uploader`` or an explicit ``users_repo().create``)."""
    from src.repositories import resource_grants_repo, user_group_members_repo, user_groups_repo

    grp = user_groups_repo().create(name=group_name, description="test", created_by="test-fixture")
    user_group_members_repo().add_member(member_user_id, grp["id"], source="admin", added_by="test-fixture")
    resource_grants_repo().create(grp["id"], "collection", collection_id, "test-fixture", "required")


def _seed_uploader(user_id: str) -> None:
    """Fixtures must be uploaded by an account that is NOT the probed
    caller (spec §5) — ownership unions into a dict user's readable set, so
    a fixture uploaded as the probed user is readable regardless of grants
    and the test would pass vacuously."""
    from src.repositories import users_repo

    users_repo().create(id=user_id, email=f"{user_id}@test.com", name=user_id)


def _seed_collection(*, collection_id: str, created_by: str) -> str:
    from src.repositories import file_corpora_repo

    with_id = file_corpora_repo()
    # file_corpora.create mints its own id; insert directly so tests control
    # the id used for grants/claims below.
    import sqlalchemy as sa

    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :slug, :name, :by)"),
            {"id": collection_id, "slug": collection_id, "name": collection_id, "by": created_by},
        )
    del with_id
    return collection_id


def _seed_corpus_file(*, corpus_id: str, file_id: str, sha256: str = "sha1") -> None:
    from src.repositories import corpus_files_repo
    import sqlalchemy as sa

    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO corpus_files (id, corpus_id, filename, sha256) "
                "VALUES (:id, :corpus_id, :filename, :sha256)"
            ),
            {"id": file_id, "corpus_id": corpus_id, "filename": f"{file_id}.md", "sha256": sha256},
        )
    del corpus_files_repo  # keep import for parity with the pattern; unused directly


def _seed_full_fixture(uploader="uploader1"):
    """One collection (CORPUS_A), one file, uploaded by ``uploader`` (NOT
    the probed caller in any S-test below)."""
    _seed_uploader(uploader)
    _seed_collection(collection_id=CORPUS_A, created_by=uploader)
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")


# ---------------------------------------------------------------------------
# S1 — a collection granted only to group C contributes nothing to Alice in
# group B: no fact, no quote, no paraphrase, no acknowledgement.
# ---------------------------------------------------------------------------


def test_s1_ungranted_collection_contributes_nothing(pg_env, repo):
    """S1. Fails if the planted value or "something exists that you cannot
    see" appears in any form — a bare empty result is the only pass."""
    _seed_full_fixture()
    fact_id = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=fact_id, type="engagement", natural_key="engagement:secret-project")
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Secret Project kicked off in March.",
        attrs={"status": "active"},
    )

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(
        pg_env, group_name="group-b", collection_id="col_other_never_granted", member_user_id="alice"
    )

    result = repo.search(_dict_user("alice"), type="engagement")
    assert result["subjects"] == []

    # Direct claims lookup on the fact must 404 (never leak existence).
    from src.repositories.facts_pg import FactNotFound

    with pytest.raises(FactNotFound):
        repo.claims(_dict_user("alice"), fact_id)


# ---------------------------------------------------------------------------
# S2 — the attribute oracle.
# ---------------------------------------------------------------------------


def test_s2_attribute_oracle_is_closed(pg_env, repo):
    """S2. One fact, two claims: one readable (existence only), one not
    (carrying attrs.price). Alice sees the fact without `price`, and
    `search(filters={price: ...})` returns no match. This is the test rev 1
    of the design would have failed."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    fact_id = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=fact_id, type="engagement", natural_key="engagement:oracle-target")
    # Readable claim: existence only, no price.
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="The engagement is underway.",
        attrs={},
    )
    # Unreadable claim: carries the price.
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_b1",
        corpus_id=CORPUS_B,
        file_sha256="sha1",
        quote="The contract value is $412,000.",
        attrs={"price": 412000},
    )

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(pg_env, group_name="group-a", collection_id=CORPUS_A, member_user_id="alice")

    result = repo.search(_dict_user("alice"), type="engagement")
    assert len(result["subjects"]) == 1
    assert "price" not in result["subjects"][0]["attrs"]

    filtered = repo.search(_dict_user("alice"), type="engagement", filters={"price": 412000})
    assert filtered["subjects"] == []


# ---------------------------------------------------------------------------
# S3 — edge visibility is never inferred from endpoints.
# ---------------------------------------------------------------------------


def test_s3_edge_with_only_unreadable_claim_is_never_returned(pg_env, repo):
    """S3. An edge whose only claim is in an unreadable collection, between
    two readable facts: fact_neighbors never returns it. Fails if edge
    visibility is inferred from endpoint visibility."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    fact_a = repo.create_fact(type="person")
    fact_b = repo.create_fact(type="person")
    repo.add_claim(
        fact_id=fact_a, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Person A exists."
    )
    repo.add_claim(
        fact_id=fact_b, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Person B exists."
    )
    edge_id = repo.create_edge(src=fact_a, type="knows", dst=fact_b)
    # ONLY claim on the edge lives in the unreadable collection.
    repo.add_claim(edge_id=edge_id, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="sha1", quote="A knows B.")

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(pg_env, group_name="group-a", collection_id=CORPUS_A, member_user_id="alice")

    result = repo.neighbors(_dict_user("alice"), fact_a)
    edge_ids = {e["id"] for e in result["edges"]}
    assert edge_id not in edge_ids
    # B is still unreachable AS A NEIGHBOR of A via this edge (no other path).
    node_ids = {n["id"] for n in result["nodes"]}
    assert fact_b not in node_ids


# ---------------------------------------------------------------------------
# S4 — traversal does not tunnel.
# ---------------------------------------------------------------------------


def test_s4_traversal_does_not_reveal_continuation_past_an_unreadable_node(pg_env, repo):
    """S4. A->B->C where C has GENUINELY no readable evidence anywhere — not
    its own claim, and not any incident edge's claim either — returns A,B
    and does not reveal that a path continues to C. (Refined per spec §4 rev
    3.2: since an edge's readable claim now evidences its endpoints too, the
    B-C edge's OWN claim must ALSO be unreadable here for this to still be a
    genuine tunnel — see the companion test right below for the case where
    it IS readable.)"""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    fact_a = repo.create_fact(type="person")
    fact_b = repo.create_fact(type="person")
    fact_c = repo.create_fact(type="person")
    for f in (fact_a, fact_b):
        repo.add_claim(fact_id=f, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote=f"{f} exists.")
    # C's only claim is in the unreadable collection.
    repo.add_claim(fact_id=fact_c, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="sha1", quote="C exists.")

    edge_ab = repo.create_edge(src=fact_a, type="knows", dst=fact_b)
    repo.add_claim(edge_id=edge_ab, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="A knows B.")
    edge_bc = repo.create_edge(src=fact_b, type="knows", dst=fact_c)
    # Edge B-C's claim is ALSO in the unreadable collection — C has zero
    # readable evidence anywhere, own or incident.
    repo.add_claim(edge_id=edge_bc, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="sha1", quote="B knows C.")

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(pg_env, group_name="group-a", collection_id=CORPUS_A, member_user_id="alice")

    result = repo.neighbors(_dict_user("alice"), fact_a, depth=2)
    node_ids = {n["id"] for n in result["nodes"]}
    edge_ids = {e["id"] for e in result["edges"]}
    assert node_ids == {fact_a, fact_b}
    assert edge_ids == {edge_ab}
    assert edge_bc not in edge_ids
    assert fact_c not in node_ids


def test_s4_refined_endpoint_evidence_reveals_a_node_via_its_incident_edges_readable_claim(pg_env, repo):
    """S4 refined (spec §4 rev 3.2, found by Run P): SAME A->B->C shape as
    the test above, except the B-C edge's OWN claim IS readable this time —
    C's own claim stays unreadable, but the edge now evidences C's existence
    too, so C and the edge ARE revealed. This is not tunneling: the caller
    can already read the exact claim ("B knows C.") that names C; hiding C
    itself would be inconsistent, not safer."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    fact_a = repo.create_fact(type="person")
    fact_b = repo.create_fact(type="person")
    fact_c = repo.create_fact(type="person")
    for f in (fact_a, fact_b):
        repo.add_claim(fact_id=f, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote=f"{f} exists.")
    repo.add_claim(fact_id=fact_c, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="sha1", quote="C exists.")

    edge_ab = repo.create_edge(src=fact_a, type="knows", dst=fact_b)
    repo.add_claim(edge_id=edge_ab, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="A knows B.")
    edge_bc = repo.create_edge(src=fact_b, type="knows", dst=fact_c)
    # The B-C edge's OWN claim is readable — it evidences C's existence too.
    repo.add_claim(edge_id=edge_bc, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="B knows C.")

    from src.repositories import users_repo

    users_repo().create(id="bella", email="bella@test.com", name="Bella")
    _make_group_with_grant(pg_env, group_name="group-bella", collection_id=CORPUS_A, member_user_id="bella")

    result = repo.neighbors(_dict_user("bella"), fact_a, depth=2)
    node_ids = {n["id"] for n in result["nodes"]}
    edge_ids = {e["id"] for e in result["edges"]}
    assert node_ids == {fact_a, fact_b, fact_c}
    assert edge_ids == {edge_ab, edge_bc}


# ---------------------------------------------------------------------------
# Endpoint-only facts (spec §4 rev 3.2) — a fact with ZERO own claims,
# visible only through a readable incident edge's claim (the exact shape of
# the Run P live failure: nodes created purely to anchor an evidenced edge).
# ---------------------------------------------------------------------------


def _seed_endpoint_only_fixture(repo, *, readable_edge_claim: bool):
    """A src fact with its own readable claim (CORPUS_A), an edge to a dst
    fact that NEVER receives an own claim (the endpoint-only case), and the
    edge's claim placed in CORPUS_A (readable) or CORPUS_B (unreadable) per
    ``readable_edge_claim``."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    src = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=src, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Acme.")
    dst = repo.create_fact(type="industry")
    corpus_id = CORPUS_A if readable_edge_claim else CORPUS_B
    file_id = "cf_a1" if readable_edge_claim else "cf_b1"
    # The alias's only justification is the SAME evidence that names the
    # industry ("Acme is a SaaS company.", below) — its provenance corpus
    # must match the anchoring edge's claim, not be unconditional.
    repo.add_alias(fact_id=dst, type="industry", natural_key="industry:saas", corpus_id=corpus_id)
    edge_id = repo.create_edge(src=src, type="works_in_industry", dst=dst)
    repo.add_claim(
        edge_id=edge_id,
        corpus_file_id=file_id,
        corpus_id=corpus_id,
        file_sha256="sha1",
        quote="Acme is a SaaS company.",
    )
    return src, dst, edge_id


def test_endpoint_only_fact_visible_in_search_via_readable_edge_claim(pg_env, repo):
    """Bullet 1: an endpoint-only fact (never given its own claim) IS
    visible in search() to a caller who can read the anchoring edge's claim,
    and serves attrs: {} — attrs stay own-claims-only (S2's attribute-oracle
    guarantee untouched)."""
    _src, dst, _edge = _seed_endpoint_only_fixture(repo, readable_edge_claim=True)

    from src.repositories import users_repo

    users_repo().create(id="jules", email="jules@test.com", name="Jules")
    _make_group_with_grant(pg_env, group_name="group-jules", collection_id=CORPUS_A, member_user_id="jules")

    result = repo.search(_dict_user("jules"), type="industry")
    subjects = {s["id"]: s for s in result["subjects"]}
    assert dst in subjects
    assert subjects[dst]["attrs"] == {}
    assert subjects[dst]["claim_count"] == 0  # own-claims-only, matching claims()


def test_endpoint_only_fact_visible_in_neighbors_via_readable_edge_claim(pg_env, repo):
    """Bullet 1: neighbors() from the src also reaches the endpoint-only
    dst — this is exactly the shape of the live Run P failure."""
    src, dst, edge_id = _seed_endpoint_only_fixture(repo, readable_edge_claim=True)

    from src.repositories import users_repo

    users_repo().create(id="kara", email="kara@test.com", name="Kara")
    _make_group_with_grant(pg_env, group_name="group-kara", collection_id=CORPUS_A, member_user_id="kara")

    result = repo.neighbors(_dict_user("kara"), src)
    assert {n["id"] for n in result["nodes"]} == {src, dst}
    assert {e["id"] for e in result["edges"]} == {edge_id}


def test_endpoint_only_fact_claims_returns_empty_list_not_404(pg_env, repo):
    """Bullet 1: claims() on a VISIBLE endpoint-only fact is a 200 with an
    empty claims list, not a 404 — the visibility GATE uses the union, the
    list itself stays own-claims-only."""
    _src, dst, _edge = _seed_endpoint_only_fixture(repo, readable_edge_claim=True)

    from src.repositories import users_repo

    users_repo().create(id="liam", email="liam@test.com", name="Liam")
    _make_group_with_grant(pg_env, group_name="group-liam", collection_id=CORPUS_A, member_user_id="liam")

    result = repo.claims(_dict_user("liam"), dst)
    assert result == {"claims": [], "revealed": False}


def test_endpoint_only_fact_hidden_when_edge_claim_unreadable(pg_env, repo):
    """Bullet 2: S3 discipline extended to endpoints — no existence leak.
    When the anchoring edge's ONLY claim lives in an unreadable collection,
    the endpoint-only fact stays invisible everywhere (search AND claims)."""
    _src, dst, _edge = _seed_endpoint_only_fixture(repo, readable_edge_claim=False)

    from src.repositories import users_repo
    from src.repositories.facts_pg import FactNotFound

    users_repo().create(id="mona", email="mona@test.com", name="Mona")
    _make_group_with_grant(pg_env, group_name="group-mona", collection_id=CORPUS_A, member_user_id="mona")

    result = repo.search(_dict_user("mona"), type="industry")
    assert result["subjects"] == []
    with pytest.raises(FactNotFound):
        repo.claims(_dict_user("mona"), dst)


# ---------------------------------------------------------------------------
# S5 — a restricted AgentPrincipal sees the subset only, on EVERY read path.
# ---------------------------------------------------------------------------


def test_s5_agent_principal_scoped_subset_search(pg_env, repo):
    """S5 (search). Fails if any path reaches for `can_access_collection`
    with the owner id (which would elevate to the owner's full authority)."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    fact_a = repo.create_fact(type="engagement")
    fact_b = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_a, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="A.")
    repo.add_claim(fact_id=fact_b, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="sha1", quote="B.")

    # Owner (the "real" identity behind the agent) can reach BOTH collections
    # — the agent's OWN scope is narrower, and that narrowing must hold even
    # though the owner is not restricted.
    principal = _agent_principal([CORPUS_A])
    result = repo.search(principal, type="engagement")
    ids = {s["id"] for s in result["subjects"]}
    assert ids == {fact_a}


def test_s5_agent_principal_scoped_subset_neighbors(pg_env, repo):
    """S5 (neighbors)."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    fact_a = repo.create_fact(type="person")
    fact_b = repo.create_fact(type="person")
    repo.add_claim(fact_id=fact_a, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="A.")
    repo.add_claim(fact_id=fact_b, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="B.")
    edge_id = repo.create_edge(src=fact_a, type="knows", dst=fact_b)
    # Edge claim lives ONLY in the collection the agent cannot reach.
    repo.add_claim(edge_id=edge_id, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="sha1", quote="A knows B.")

    principal = _agent_principal([CORPUS_A])
    result = repo.neighbors(principal, fact_a)
    assert result["edges"] == []


def test_s5_agent_principal_scoped_subset_claims(pg_env, repo):
    """S5 (claims) — 404, not an empty/partial list, for a subject with zero
    readable claims under the agent's scope."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="sha1", quote="B.")

    from src.repositories.facts_pg import FactNotFound

    principal = _agent_principal(["col_other"])
    with pytest.raises(FactNotFound):
        repo.claims(principal, fact_id)


# ---------------------------------------------------------------------------
# S6 — no existence oracle.
# ---------------------------------------------------------------------------


def test_s6_nonexistent_and_unreadable_ids_404_identically(pg_env, repo):
    """S6 (identity of 404s). A nonexistent id and a no-readable-claim id
    must raise the same exception type/shape — the REST layer maps both to
    an identical 404 body."""
    _seed_full_fixture()
    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Hidden.")

    from src.repositories import users_repo
    from src.repositories.facts_pg import FactNotFound

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    # Alice has NO grants at all.

    with pytest.raises(FactNotFound) as exc_no_claim:
        repo.claims(_dict_user("alice"), fact_id)
    with pytest.raises(FactNotFound) as exc_nonexistent:
        repo.claims(_dict_user("alice"), "f_does_not_exist_at_all")

    # Same exception type, same constructor shape (subject_id is not part of
    # any externally-serialized 404 body — the REST layer never echoes it).
    assert type(exc_no_claim.value) is type(exc_nonexistent.value)


def test_s6_limit_shortfall_is_not_signaled(pg_env, repo):
    """S6 (shortfall oracle). limit=20 where 50 facts match the type but
    only 5 are readable by the caller returns exactly 5, with
    limit_applied=False — nothing distinguishes "RBAC filtered the rest"
    from "there simply were only 5"."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    for i in range(5):
        fid = repo.create_fact(type="engagement")
        repo.add_claim(
            fact_id=fid, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote=f"readable {i}"
        )
    for i in range(45):
        fid = repo.create_fact(type="engagement")
        repo.add_claim(
            fact_id=fid, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="sha1", quote=f"unreadable {i}"
        )

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(pg_env, group_name="group-a", collection_id=CORPUS_A, member_user_id="alice")

    result = repo.search(_dict_user("alice"), type="engagement", limit=20)
    assert len(result["subjects"]) == 5
    assert result["limit_applied"] is False


def test_s6_limit_applied_true_when_the_callers_own_visible_set_is_truncated(pg_env, repo):
    """Companion to the shortfall test: when MORE than `limit` subjects are
    genuinely visible to the caller, limit_applied is True — this is a
    legitimate signal about the caller's OWN result set, never about what
    RBAC hid."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    for i in range(10):
        _seed_corpus_file(corpus_id=CORPUS_A, file_id=f"cf_a{i}")
        fid = repo.create_fact(type="engagement")
        repo.add_claim(
            fact_id=fid, corpus_file_id=f"cf_a{i}", corpus_id=CORPUS_A, file_sha256="sha1", quote=f"visible {i}"
        )

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(pg_env, group_name="group-a", collection_id=CORPUS_A, member_user_id="alice")

    result = repo.search(_dict_user("alice"), type="engagement", limit=5)
    assert len(result["subjects"]) == 5
    assert result["limit_applied"] is True


# ---------------------------------------------------------------------------
# Q — free-text name lookup against fact_aliases.natural_key (TCRD follow-up:
# the search API had no free-text parameter at all, so an unknown `q` field
# was silently swallowed by pydantic's default extra='ignore' and the call
# degenerated to an unfiltered, id-ordered dump).
# ---------------------------------------------------------------------------


def test_q_ranks_the_matching_subject_first(pg_env, repo):
    """Ordering-sensitive: three subjects, three distinct `q` values — each
    query must return ITS planted subject first, not merely somewhere in an
    id-ordered dump. This is the test that fails against the pre-`q` code
    (which ignores `q` entirely and orders by `v.subject_id`, a random hex
    id with no relation to any query)."""
    _seed_full_fixture()

    parts_authority = repo.create_fact(type="organization")
    repo.add_alias(
        fact_id=parts_authority, type="organization", natural_key="organization:parts-authority", corpus_id=CORPUS_A
    )
    repo.add_claim(
        fact_id=parts_authority,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Parts Authority is a client.",
    )

    alpha_logistics = repo.create_fact(type="organization")
    repo.add_alias(
        fact_id=alpha_logistics, type="organization", natural_key="organization:alpha-logistics", corpus_id=CORPUS_A
    )
    repo.add_claim(
        fact_id=alpha_logistics,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Alpha Logistics is a client.",
    )

    beta_industries = repo.create_fact(type="organization")
    repo.add_alias(
        fact_id=beta_industries, type="organization", natural_key="organization:beta-industries", corpus_id=CORPUS_A
    )
    repo.add_claim(
        fact_id=beta_industries,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Beta Industries is a client.",
    )

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(pg_env, group_name="group-a", collection_id=CORPUS_A, member_user_id="alice")

    # Natural-language queries (spaces, mixed case) must normalize
    # (casefold + spaces->hyphens) to match the `<type>:<kebab-slug>` alias.
    for query, expected_first in (
        ("Parts Authority", parts_authority),
        ("Alpha Logistics", alpha_logistics),
        ("Beta Industries", beta_industries),
    ):
        result = repo.search(_dict_user("alice"), type="organization", q=query)
        assert result["subjects"], f"q={query!r} returned nothing"
        assert result["subjects"][0]["id"] == expected_first, (
            f"q={query!r} did not rank its planted subject first: got {result['subjects']}"
        )

    # `q` is optional -- the unfiltered call still returns every visible
    # subject (order-independent check; ordering is q's job, not the
    # default's).
    unfiltered = repo.search(_dict_user("alice"), type="organization")
    assert {s["id"] for s in unfiltered["subjects"]} == {parts_authority, alpha_logistics, beta_industries}


def test_q_filters_out_non_matching_subjects(pg_env, repo):
    """`q` is a filter, not merely a sort key: a subject with no alias
    matching `q` must not appear at all, even though it is otherwise fully
    visible (S6 shortfall rule: filtering happens PRE-limit in SQL)."""
    _seed_full_fixture()

    matching = repo.create_fact(type="organization")
    repo.add_alias(
        fact_id=matching, type="organization", natural_key="organization:parts-authority", corpus_id=CORPUS_A
    )
    repo.add_claim(
        fact_id=matching, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Parts Authority."
    )

    other = repo.create_fact(type="organization")
    repo.add_alias(fact_id=other, type="organization", natural_key="organization:zephyr-corp", corpus_id=CORPUS_A)
    repo.add_claim(fact_id=other, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Zephyr Corp.")

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(pg_env, group_name="group-a", collection_id=CORPUS_A, member_user_id="alice")

    result = repo.search(_dict_user("alice"), type="organization", q="parts")
    assert [s["id"] for s in result["subjects"]] == [matching]


def test_q_never_matches_claim_text_only_aliases(pg_env, repo):
    """`q` matches `fact_aliases.natural_key` ONLY -- never claim quotes or
    attrs -- so it can never reopen the S2 attribute oracle. A word that
    appears solely in a claim's quote must not surface the subject."""
    _seed_full_fixture()

    fact_id = repo.create_fact(type="organization")
    repo.add_alias(fact_id=fact_id, type="organization", natural_key="organization:generic-co")
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="This company handles unobtainium logistics exclusively.",
    )

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(pg_env, group_name="group-a", collection_id=CORPUS_A, member_user_id="alice")

    result = repo.search(_dict_user("alice"), type="organization", q="unobtainium")
    assert result["subjects"] == []


def test_q_exact_match_ranks_above_prefix_and_substring_matches(pg_env, repo):
    """Deterministic non-extension ranking tiers: an exact slug match beats
    a prefix match, which beats a plain substring match."""
    _seed_full_fixture()

    exact = repo.create_fact(type="organization")
    repo.add_alias(fact_id=exact, type="organization", natural_key="organization:acme", corpus_id=CORPUS_A)
    repo.add_claim(fact_id=exact, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Acme.")

    prefix = repo.create_fact(type="organization")
    repo.add_alias(fact_id=prefix, type="organization", natural_key="organization:acme-holdings", corpus_id=CORPUS_A)
    repo.add_claim(
        fact_id=prefix, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Acme Holdings."
    )

    substring = repo.create_fact(type="organization")
    repo.add_alias(
        fact_id=substring, type="organization", natural_key="organization:new-acme-ventures", corpus_id=CORPUS_A
    )
    repo.add_claim(
        fact_id=substring, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="New Acme Ventures."
    )

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(pg_env, group_name="group-a", collection_id=CORPUS_A, member_user_id="alice")

    result = repo.search(_dict_user("alice"), type="organization", q="acme")
    ids = [s["id"] for s in result["subjects"]]
    assert ids == [exact, prefix, substring]


def test_q_empty_string_is_treated_as_absent(pg_env, repo):
    """A blank/whitespace-only `q` degrades to "no free-text filter" rather
    than matching everything or nothing surprising."""
    _seed_full_fixture()
    fact_id = repo.create_fact(type="organization")
    repo.add_alias(fact_id=fact_id, type="organization", natural_key="organization:solo-co")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Solo Co.")

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(pg_env, group_name="group-a", collection_id=CORPUS_A, member_user_id="alice")

    result = repo.search(_dict_user("alice"), type="organization", q="   ")
    assert [s["id"] for s in result["subjects"]] == [fact_id]


def test_q_escapes_like_metacharacters(pg_env, repo):
    """A literal `%`/`_` in `q` must be treated as a literal character, not
    a LIKE wildcard -- an unescaped `_` would match any single character."""
    _seed_full_fixture()

    literal = repo.create_fact(type="organization")
    repo.add_alias(fact_id=literal, type="organization", natural_key="organization:100%-co", corpus_id=CORPUS_A)
    repo.add_claim(fact_id=literal, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="100%-Co.")

    decoy = repo.create_fact(type="organization")
    repo.add_alias(fact_id=decoy, type="organization", natural_key="organization:100xco", corpus_id=CORPUS_A)
    repo.add_claim(fact_id=decoy, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="100xCo.")

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(pg_env, group_name="group-a", collection_id=CORPUS_A, member_user_id="alice")

    result = repo.search(_dict_user("alice"), type="organization", q="100%-co")
    assert [s["id"] for s in result["subjects"]] == [literal]


# ---------------------------------------------------------------------------
# P2 review finding: `q` has no repo-layer floor, so a 1-char query drives a
# full-scan ILIKE — and `search()` runs with no statement timeout at all,
# unlike `neighbors()`. Enforced at the REPOSITORY layer (not merely the
# REST Pydantic model) so an MCP/CLI caller reaching `search()` directly
# cannot bypass it either.
# ---------------------------------------------------------------------------


def test_q_below_minimum_length_is_refused(pg_env, repo):
    """A 1-char `q` is refused outright rather than driving an unbounded
    ILIKE scan — mirrors the `too many filters` ValueError contract (the
    REST layer already translates a bare `ValueError` to a `422`)."""
    _seed_full_fixture()
    with pytest.raises(ValueError):
        repo.search(_dict_user("alice"), type="organization", q="a")


def test_q_blank_is_exempt_from_the_minimum_length(pg_env, repo):
    """A blank/whitespace `q` still degrades to "no filter" (existing
    contract, `test_q_empty_string_is_treated_as_absent`) rather than
    tripping the new floor — the floor only applies to a caller-supplied,
    non-blank, too-short query."""
    _seed_full_fixture()
    fact_id = repo.create_fact(type="organization")
    repo.add_alias(fact_id=fact_id, type="organization", natural_key="organization:solo-co2")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Solo Co 2.")

    from src.repositories import users_repo

    users_repo().create(id="quinn", email="quinn@test.com", name="Quinn")
    _make_group_with_grant(pg_env, group_name="group-q", collection_id=CORPUS_A, member_user_id="quinn")

    result = repo.search(_dict_user("quinn"), type="organization", q="  ")
    assert [s["id"] for s in result["subjects"]] == [fact_id]


def test_search_applies_a_statement_timeout(pg_env, repo):
    """`search()` must bound its query the same way `neighbors()` does — a
    single ILIKE-driven candidate scan with no bound would let a caller
    stall a connection out of the pool. The generic Postgres mechanism
    (`SET LOCAL statement_timeout` genuinely cancelling a slow statement) is
    proven once, directly, by `test_statement_timeout_mechanism_actually_
    cancels` below; this test proves `search()` actually WIRES it, by
    recording every statement issued on the connection it opens."""
    from sqlalchemy import event

    _seed_full_fixture()
    statements: list = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(repo._engine, "before_cursor_execute", _capture)
    try:
        repo.search(_dict_user("bystander"), type="organization")
    finally:
        event.remove(repo._engine, "before_cursor_execute", _capture)

    assert any("SET LOCAL statement_timeout" in s for s in statements)


# ---------------------------------------------------------------------------
# S8 (read side) — corrections enforced at read time.
# ---------------------------------------------------------------------------


def test_s8_revealed_serves_without_quotes_regardless_of_grants(pg_env, repo):
    """S8. A `revealed` correction serves the fact instance-wide, without
    quotes, to a caller with ZERO grants on the evidencing collection."""
    _seed_full_fixture()
    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="The engagement went ahead as planned.",
        attrs={"status": "active"},
    )
    repo.upsert_correction(
        subject_kind="fact",
        subject_id=fact_id,
        natural_keys={"aliases": []},
        verdict="revealed",
        reason="publicly announced",
        decided_by="admin1",
    )

    from src.repositories import users_repo

    users_repo().create(id="bob", email="bob@test.com", name="Bob")
    # Bob has NO grants at all.

    result = repo.search(_dict_user("bob"), type="engagement")
    assert len(result["subjects"]) == 1
    subject = result["subjects"][0]
    assert subject["revealed"] is True
    assert subject["quote_count"] == 0
    assert subject["attrs"]["status"]["value"] == "active"

    claims_result = repo.claims(_dict_user("bob"), fact_id)
    assert claims_result["revealed"] is True
    assert len(claims_result["claims"]) == 1
    assert claims_result["claims"][0]["quote"] == ""
    # Review tightening (spec §4, 2026-08-28): revealed reveals the FACT,
    # not the geography of its evidence — an ungranted caller gets NO
    # document name/path/URL for the unreadable claim, opaque ids only.
    assert claims_result["claims"][0]["document"] is None
    assert claims_result["claims"][0]["corpus_file_id"] == "cf_a1"

    # A caller who CAN read the evidencing collection (the uploader owns
    # it — ownership unions into a dict user's readable set) keeps full
    # document identity on the same revealed subject.
    owner_claims = repo.claims(_dict_user("uploader1"), fact_id)
    assert owner_claims["claims"][0]["document"] is not None
    assert owner_claims["claims"][0]["document"]["name"]


def test_s8_restricted_hides_from_a_caller_with_full_grants(pg_env, repo):
    """S8. `restricted` withholds the subject even from a caller who holds a
    normal, full grant on the evidencing collection (legal hold /
    personnel — reach beats grants)."""
    _seed_full_fixture()
    fact_id = repo.create_fact(type="person")
    repo.add_claim(
        fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Sensitive record."
    )
    repo.upsert_correction(
        subject_kind="fact",
        subject_id=fact_id,
        natural_keys={"aliases": []},
        verdict="restricted",
        reason="legal hold",
        decided_by="admin1",
    )

    from src.repositories import users_repo
    from src.repositories.facts_pg import FactNotFound

    users_repo().create(id="carol", email="carol@test.com", name="Carol")
    _make_group_with_grant(pg_env, group_name="group-full", collection_id=CORPUS_A, member_user_id="carol")

    result = repo.search(_dict_user("carol"), type="person")
    assert fact_id not in {s["id"] for s in result["subjects"]}

    with pytest.raises(FactNotFound):
        repo.claims(_dict_user("carol"), fact_id)


def test_s8_wrong_hides_at_read_time(pg_env, repo):
    """`wrong` is withheld everywhere, same as `restricted`, from the read
    path's point of view (the write-side "survives re-ingest" half of S8
    belongs to the ingest task)."""
    _seed_full_fixture()
    fact_id = repo.create_fact(type="person")
    repo.add_claim(
        fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Incorrect claim."
    )
    repo.upsert_correction(
        subject_kind="fact",
        subject_id=fact_id,
        natural_keys={"aliases": []},
        verdict="wrong",
        reason="hallucinated",
        decided_by="admin1",
    )

    from src.repositories import users_repo
    from src.repositories.facts_pg import FactNotFound

    users_repo().create(id="carol", email="carol@test.com", name="Carol")
    _make_group_with_grant(pg_env, group_name="group-wrong", collection_id=CORPUS_A, member_user_id="carol")

    result = repo.search(_dict_user("carol"), type="person")
    assert fact_id not in {s["id"] for s in result["subjects"]}
    with pytest.raises(FactNotFound):
        repo.claims(_dict_user("carol"), fact_id)


def _admin() -> dict:
    """The caller shape returned by :func:`_seed_admin` below — a bare
    ``{"id": "admin1"}`` with no membership row would resolve as an
    ORDINARY ungranted user (``accessible_collection_ids`` checks a real DB
    membership, src.rbac.get_accessible_ids), so every S9 test using this
    must call :func:`_seed_admin` first."""
    return {"id": "admin1", "email": "admin@test.com"}


def _seed_admin(pg_engine) -> None:
    """Seed ``admin1`` as a REAL Admin-group member (mirrors
    ``test_facts_ingest_pg.py``'s ``pg_env`` fixture) — this file's own
    ``pg_env`` only seeds the ``user_groups`` rows via
    ``_seed_pg_system_groups``, not the membership itself."""
    import sqlalchemy as sa

    from src.repositories import user_group_members_repo, users_repo

    users_repo().create(id="admin1", email="admin@test.com", name="Admin")
    with pg_engine.connect() as conn:
        admin_gid = conn.execute(sa.text("SELECT id FROM user_groups WHERE name = 'Admin'")).scalar()
    user_group_members_repo().add_member("admin1", admin_gid, source="system_seed")


# ---------------------------------------------------------------------------
# S9 — the alias oracle (security hardening, 2026-08-29): a fact's own
# claims being PARTLY readable does not make every one of its aliases
# readable. The reproduction below plants the exact shape a live eval found:
# one fact, one claim in a readable collection that does NOT name it, one
# claim in an unreadable collection that DOES (the alias was minted from
# it). See docs/superpowers/specs/2026-08-27-fact-graph-over-collections-
# design.md §4/§5, §15.1 S9.
# ---------------------------------------------------------------------------

CORPUS_RESTRICTED = "col_restricted"


def _seed_ingest_ready_doc(*, corpus_id: str, file_id: str, doc_id: str, text: str) -> str:
    """Minimal indexed ``corpus_file`` + chunk + ``doc_id`` mapping so
    ``ingest_batch`` can resolve evidence against it (mirrors
    ``test_facts_ingest_pg.py``'s ``_seed_ready_doc``). Needed only by the
    S9 tests below that exercise the REAL write path
    (``repo.ingest_batch``) rather than the low-level ``add_claim``/
    ``add_alias`` primitives every other S9 fixture uses — the edge-anchor
    provenance bug lives in ``_write_evidence``'s ``alias_targets``
    wiring, which the low-level primitives never touch at all."""
    import secrets

    import sqlalchemy as sa

    from src.db_pg import get_engine
    from src.repositories import corpus_file_sources_repo

    with get_engine().begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO corpus_files (id, corpus_id, filename, sha256, processing_status) "
                "VALUES (:id, :corpus_id, :filename, :sha256, 'indexed')"
            ),
            {"id": file_id, "corpus_id": corpus_id, "filename": f"{file_id}.md", "sha256": f"sha_{file_id}"},
        )
        conn.execute(
            sa.text(
                "INSERT INTO corpus_chunks (id, corpus_id, file_id, ordinal, text) "
                "VALUES (:id, :corpus_id, :file_id, 0, :text)"
            ),
            {"id": "ck_" + secrets.token_hex(8), "corpus_id": corpus_id, "file_id": file_id, "text": text},
        )
    corpus_file_sources_repo().upsert(
        corpus_file_id=file_id, corpus_id=corpus_id, source_stable_id=file_id, source_doc_id=doc_id
    )
    return doc_id


def _seed_alias_oracle_fixture(repo):
    """One fact (``engagement:halyard-erp-rollout`` — the alias minted from
    the RESTRICTED collection's claim, which names the client), one claim
    in CORPUS_A (readable to Alice, does not name the client) and one claim
    in CORPUS_RESTRICTED (unreadable to Alice, the one that actually named
    it — same shape as ``add_alias_source`` would be populated by a real
    ``ingest_batch`` node whose evidence spans two collections)."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_RESTRICTED, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_RESTRICTED, file_id="cf_r1")

    fact_id = repo.create_fact(type="engagement")
    repo.add_alias(
        fact_id=fact_id,
        type="engagement",
        natural_key="engagement:halyard-erp-rollout",
        corpus_id=CORPUS_RESTRICTED,
    )
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Dax Okonkwo-Reyes is the lead consultant on the engagement.",
    )
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_r1",
        corpus_id=CORPUS_RESTRICTED,
        file_sha256="sha1",
        quote="The Halyard Precision Manufacturing ERP Rollout kicked off March 1.",
    )
    return fact_id


def test_s9_alias_oracle_is_closed(pg_env, repo):
    """S9 (the reproduction). Alice is granted CORPUS_A only — she can read
    the claim naming the lead consultant (so the fact is visible at all)
    but NOT the claim the alias was minted from. ``fact_search`` must not
    show the restricted alias; the fact still carries a usable identity
    (the opaque subject id). Fails on pre-hardening code (aliases were
    joined with no grant filter at all)."""
    fact_id = _seed_alias_oracle_fixture(repo)

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(pg_env, group_name="group-alice-s9", collection_id=CORPUS_A, member_user_id="alice")

    result = repo.search(_dict_user("alice"), type="engagement")
    assert [s["id"] for s in result["subjects"]] == [fact_id]
    subject = result["subjects"][0]
    assert subject["aliases"] == []
    assert "halyard" not in str(subject).lower()


def test_s9_backfill_makes_a_legacy_alias_and_q_search_work_for_a_non_admin(pg_engine, monkeypatch, tmp_path):
    """Adversarial-review finding: ``0084_fact_alias_sources`` creates the
    table EMPTY. Without a backfill, ``_alias_readable_sql`` treats a
    zero-provenance-row alias as unreadable for every non-admin — i.e.
    every alias minted BEFORE this deploy — and ``search()``'s
    ``candidates`` CTE requires a readable alias match whenever ``q`` is
    given, so `q` would return ZERO results for every pre-existing subject
    on any instance with real fact data (an operator would have to
    re-ingest to get working search back).

    This test steps the Alembic chain itself — upgrade to
    ``0083_ingest_runs_source_urls`` (``fact_alias_sources`` doesn't exist
    yet), seed a fact/alias/claim through the SAME repo methods
    pre-deploy code used (``create_fact``/``add_claim`` with no
    ``corpus_id`` — the table isn't there to write to), THEN upgrade to
    head — so it actually exercises the migration's backfill INSERT, not
    merely the read-path filter (every other S9 test seeds provenance
    explicitly via ``add_alias(..., corpus_id=...)`` and would pass even
    if the backfill were deleted). Fails on the pre-backfill migration:
    with no backfill, both assertions below see an empty result."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "0083_ingest_runs_source_urls")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from tests.db_pg._parity_sweep_util import _seed_pg_system_groups

    _seed_pg_system_groups(pg_engine)

    from src.repositories.facts_pg import FactsPgRepository

    repo = FactsPgRepository(db_pg.get_engine())

    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")

    # Legacy data: an alias with NO fact_alias_sources row, because the
    # table doesn't exist at this revision yet — exactly the shape every
    # subject minted before this PR is in.
    fact_id = repo.create_fact(type="engagement", natural_key="engagement:legacy-rollout")
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="The legacy engagement is underway.",
    )

    command.upgrade(cfg, "head")

    from src.repositories import users_repo

    users_repo().create(id="lena", email="lena@test.com", name="Lena")
    _make_group_with_grant(pg_engine, group_name="group-lena-s9", collection_id=CORPUS_A, member_user_id="lena")

    result = repo.search(_dict_user("lena"), type="engagement")
    assert [s["id"] for s in result["subjects"]] == [fact_id]
    assert result["subjects"][0]["aliases"] == ["engagement:legacy-rollout"]

    q_result = repo.search(_dict_user("lena"), type="engagement", q="legacy-rollout")
    assert [s["id"] for s in q_result["subjects"]] == [fact_id]


def test_s9_edge_anchor_alias_gets_provenance_from_the_evidencing_edge(pg_env, repo):
    """Live-path finding (adversarial review round 2, live-data run):
    ``industry:saas-anchor`` below is never listed in ``nodes[]`` — it
    exists ONLY as an edge's ``dst`` (the ordinary
    ``works_in_industry``/``sponsored_by``/``staffed_by``-shaped ontology
    row where the evidence sits on the edge, never the node — spec §7.0,
    "nodes without evidence are warnings, every edge carries >=1
    evidence"). It carries ZERO claims of its own, so its ONLY possible
    provenance is the edge's claim. Priti can read the edge's evidencing
    corpus, so she must see BOTH the display name and match it via `q` —
    this exercises the REAL ``ingest_batch`` write path (not the
    low-level ``add_claim``/``add_alias`` primitives every other S9
    fixture uses), because the bug lives in ``_write_evidence``'s
    ``alias_targets`` wiring for the edge-evidence loop specifically.
    Fails on the unfixed edge loop: `_write_evidence(kind="edge", ...)`
    passed no alias target at all, so this endpoint's alias NEVER gets a
    ``fact_alias_sources`` row -- permanently admin-only regardless of
    which corpus evidences it."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    doc_id = _seed_ingest_ready_doc(
        corpus_id=CORPUS_A,
        file_id="cf_anchor1",
        doc_id="doc_anchor1",
        text="Acme Corp operates in the SaaS industry.",
    )

    report = repo.ingest_batch(
        nodes=[
            {
                "id": "engagement:acme-anchor",
                "type": "engagement",
                "attrs": {},
                "evidence": [{"doc_id": doc_id, "quote": "Acme Corp operates in the SaaS industry."}],
            }
        ],
        edges=[
            {
                "src": "engagement:acme-anchor",
                "type": "works_in_industry",
                "dst": "industry:saas-anchor",
                "evidence": [{"doc_id": doc_id, "quote": "Acme Corp operates in the SaaS industry."}],
            }
        ],
    )
    assert report["claims_written"] == 2  # 1 node claim + 1 edge claim
    assert report["claims_rejected"] == []

    from src.repositories import users_repo

    users_repo().create(id="priti", email="priti@test.com", name="Priti")
    _make_group_with_grant(pg_env, group_name="group-priti-s9", collection_id=CORPUS_A, member_user_id="priti")

    result = repo.search(_dict_user("priti"), type="industry")
    assert len(result["subjects"]) == 1
    subject = result["subjects"][0]
    assert subject["aliases"] == ["industry:saas-anchor"]
    assert subject["claim_count"] == 0  # own-claims-only projection, S2 — it has none

    q_result = repo.search(_dict_user("priti"), type="industry", q="saas-anchor")
    assert [s["id"] for s in q_result["subjects"]] == [subject["id"]]


def test_s9_backfill_covers_an_edge_anchor_alias_for_a_non_admin(pg_engine, monkeypatch, tmp_path):
    """The 0084 backfill's edge-anchor half (adversarial review round 2):
    steps the Alembic chain to ``0083_ingest_runs_source_urls`` (before
    ``fact_alias_sources`` exists), seeds the pre-existing
    ``facts``/``fact_aliases``/``edges``/``claims`` rows through the
    low-level primitives (``create_fact``/``create_edge``/``add_claim``,
    no ``corpus_id`` kwarg — the CURRENT ``ingest_batch`` unconditionally
    writes to ``fact_alias_sources`` now, so it cannot run against a
    schema that doesn't have the table yet; these primitives are the ones
    that don't touch it, exactly matching what pre-deploy code would have
    left behind), THEN upgrades to head. The backfill must credit the
    anchor's alias from its incident edge's claim, not just claims on its
    own ``fact_id`` (which it has none of) — the exact gap the live-data
    run on agnes-dev surfaced (20 of 81 aliases, all zero-own-claim edge
    anchors). Fails on a backfill that only joins
    ``claims ON claims.fact_id = fact_aliases.fact_id``."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "0083_ingest_runs_source_urls")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from tests.db_pg._parity_sweep_util import _seed_pg_system_groups

    _seed_pg_system_groups(pg_engine)

    from src.repositories.facts_pg import FactsPgRepository

    repo = FactsPgRepository(db_pg.get_engine())

    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_legacy_anchor1")

    # Legacy shape: a src fact WITH its own claim, an edge to a dst fact
    # that NEVER gets a claim of its own (zero own claims — the
    # edge-anchor shape), the edge's own claim in the readable corpus.
    src = repo.create_fact(type="engagement", natural_key="engagement:acme-legacy-anchor")
    dst = repo.create_fact(type="industry", natural_key="industry:legacy-saas-anchor")
    repo.add_claim(
        fact_id=src, corpus_file_id="cf_legacy_anchor1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Acme exists."
    )
    edge_id = repo.create_edge(src=src, type="works_in_industry", dst=dst)
    repo.add_claim(
        edge_id=edge_id,
        corpus_file_id="cf_legacy_anchor1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Acme operates in the legacy SaaS industry.",
    )

    command.upgrade(cfg, "head")

    from src.repositories import users_repo

    users_repo().create(id="omar", email="omar@test.com", name="Omar")
    _make_group_with_grant(pg_engine, group_name="group-omar-s9", collection_id=CORPUS_A, member_user_id="omar")

    result = repo.search(_dict_user("omar"), type="industry")
    assert len(result["subjects"]) == 1
    subject = result["subjects"][0]
    assert subject["aliases"] == ["industry:legacy-saas-anchor"]

    q_result = repo.search(_dict_user("omar"), type="industry", q="legacy-saas-anchor")
    assert [s["id"] for s in q_result["subjects"]] == [subject["id"]]


def test_s9_admin_sees_the_restricted_alias_regardless(pg_env, repo):
    """God-mode: an Admin-group caller sees every alias unconditionally —
    never gated on ``fact_alias_sources`` having a row for it either
    (an admin-visible alias must not depend on backfilled provenance
    data)."""
    _seed_admin(pg_env)
    fact_id = _seed_alias_oracle_fixture(repo)

    result = repo.search(_admin(), type="engagement")
    assert [s["id"] for s in result["subjects"]] == [fact_id]
    assert result["subjects"][0]["aliases"] == ["engagement:halyard-erp-rollout"]


def test_s9_revealed_serves_the_restricted_alias_regardless_of_grants(pg_env, repo):
    """`revealed` bypasses grants entirely (spec §4) — the SAME rule
    already applies to `attrs`/quotes; this asserts it holds for the
    alias/display name too. A caller with ZERO grants on either collection
    still sees the fact's alias once it is revealed."""
    fact_id = _seed_alias_oracle_fixture(repo)
    repo.upsert_correction(
        subject_kind="fact",
        subject_id=fact_id,
        natural_keys={"aliases": ["engagement:halyard-erp-rollout"]},
        verdict="revealed",
        reason="publicly announced",
        decided_by="admin1",
    )

    from src.repositories import users_repo

    users_repo().create(id="zack", email="zack@test.com", name="Zack")
    # Zack has NO grants at all.

    result = repo.search(_dict_user("zack"), type="engagement")
    assert [s["id"] for s in result["subjects"]] == [fact_id]
    assert result["subjects"][0]["aliases"] == ["engagement:halyard-erp-rollout"]


def test_s9_q_never_matches_a_restricted_only_alias(pg_env, repo):
    """Search-matching oracle: `q` matching an alias the caller cannot see
    must return NOTHING for that caller — a hit (or its absence) must not
    reveal whether a restricted name exists. The same query DOES match for
    a caller who can read the minting collection."""
    _seed_alias_oracle_fixture(repo)

    from src.repositories import users_repo

    users_repo().create(id="alice2", email="alice2@test.com", name="Alice2")
    _make_group_with_grant(pg_env, group_name="group-alice2-s9", collection_id=CORPUS_A, member_user_id="alice2")
    users_repo().create(id="rita", email="rita@test.com", name="Rita")
    _make_group_with_grant(pg_env, group_name="group-rita-s9", collection_id=CORPUS_RESTRICTED, member_user_id="rita")

    alice_result = repo.search(_dict_user("alice2"), type="engagement", q="Halyard")
    assert alice_result["subjects"] == []

    rita_result = repo.search(_dict_user("rita"), type="engagement", q="Halyard")
    assert len(rita_result["subjects"]) == 1
    assert rita_result["subjects"][0]["aliases"] == ["engagement:halyard-erp-rollout"]


def test_s9_neighbors_hides_the_restricted_alias_per_hop(pg_env, repo):
    """`fact_neighbors` re-evaluates the SAME alias-visibility rule at
    every hop (spec §12's shared projection) — walking the graph must not
    be a side channel back to a name `fact_search` already hides."""
    fact_id = _seed_alias_oracle_fixture(repo)
    other = repo.create_fact(type="person")
    repo.add_claim(fact_id=other, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Dax exists.")
    edge_id = repo.create_edge(src=fact_id, type="staffed_by", dst=other)
    repo.add_claim(
        edge_id=edge_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Staffed by Dax."
    )

    from src.repositories import users_repo

    users_repo().create(id="petra2", email="petra2@test.com", name="Petra2")
    _make_group_with_grant(pg_env, group_name="group-petra2-s9", collection_id=CORPUS_A, member_user_id="petra2")

    result = repo.neighbors(_dict_user("petra2"), fact_id)
    node = next(n for n in result["nodes"] if n["id"] == fact_id)
    assert node["aliases"] == []


def test_s9_merge_facts_preserves_alias_provenance_then_split_reverses_it(pg_env, repo):
    """Merge/split (spec §3, EQ7) are audit-logged alias-repoint operations
    over ``fact_aliases.fact_id`` — they must not disturb each alias's OWN
    provenance row (keyed on the alias's stable ``(type, natural_key)``,
    untouched by either operation). A caller who can read the canonical's
    minting collection but not the merged-in duplicate's still sees only
    the readable alias after merge, and the split correctly separates them
    back onto two facts with their original provenance intact."""
    _seed_admin(pg_env)
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_RESTRICTED, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_RESTRICTED, file_id="cf_r1")

    canonical_id = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=canonical_id, type="engagement", natural_key="engagement:acme-rollout", corpus_id=CORPUS_A)
    repo.add_claim(fact_id=canonical_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Acme.")

    duplicate_id = repo.create_fact(type="engagement")
    repo.add_alias(
        fact_id=duplicate_id,
        type="engagement",
        natural_key="engagement:acme-rollout-restricted-name",
        corpus_id=CORPUS_RESTRICTED,
    )
    # A SECOND, readable claim (unrelated to the alias) so the split-off
    # fact stays independently VISIBLE to Mo below — the point under test
    # is that its ALIAS stays hidden despite the fact itself being
    # visible, not that the whole fact disappears (that's S1/S6's job).
    repo.add_claim(
        fact_id=duplicate_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Acme continued."
    )
    repo.add_claim(
        fact_id=duplicate_id,
        corpus_file_id="cf_r1",
        corpus_id=CORPUS_RESTRICTED,
        file_sha256="sha1",
        quote="Acme restricted alias.",
    )

    from src.repositories import users_repo

    users_repo().create(id="mo", email="mo@test.com", name="Mo")
    _make_group_with_grant(pg_env, group_name="group-mo-s9", collection_id=CORPUS_A, member_user_id="mo")

    snapshot = repo.merge_facts(canonical_id=canonical_id, merged_id=duplicate_id, merged_by="admin1")
    merged_for_mo = repo.search(_dict_user("mo"), type="engagement")["subjects"][0]
    assert merged_for_mo["id"] == canonical_id
    assert merged_for_mo["aliases"] == ["engagement:acme-rollout"]
    merged_for_admin = repo.search(_admin(), type="engagement")["subjects"][0]
    assert set(merged_for_admin["aliases"]) == {
        "engagement:acme-rollout",
        "engagement:acme-rollout-restricted-name",
    }

    new_id = repo.split_fact(canonical_id=canonical_id, snapshot=snapshot, split_by="admin1")
    after_split = {s["id"]: s for s in repo.search(_admin(), type="engagement")["subjects"]}
    assert set(after_split) == {canonical_id, new_id}
    assert after_split[canonical_id]["aliases"] == ["engagement:acme-rollout"]
    assert after_split[new_id]["aliases"] == ["engagement:acme-rollout-restricted-name"]
    # Mo still can't read the split-off restricted-provenance fact's alias.
    mo_after_split = {s["id"]: s for s in repo.search(_dict_user("mo"), type="engagement")["subjects"]}
    assert mo_after_split[canonical_id]["aliases"] == ["engagement:acme-rollout"]
    assert mo_after_split[new_id]["aliases"] == []


# ---------------------------------------------------------------------------
# attrs projection — unit tests over search()'s SQL-side projection.
# ---------------------------------------------------------------------------


def _seed_two_claims(repo, *, dates, values, key="status"):
    """Two claims on ONE fact, both in CORPUS_A (both readable to any caller
    holding a CORPUS_A grant) — the projection scenario under test."""
    fact_id = repo.create_fact(type="engagement")
    for i, (d, v) in enumerate(zip(dates, values)):
        repo.add_claim(
            fact_id=fact_id,
            corpus_file_id="cf_a1",
            corpus_id=CORPUS_A,
            file_sha256="sha1",
            quote=f"claim {i} says {v}",
            attrs={key: v},
            document_date=d,
        )
    return fact_id


def _search_one(repo, user_id, fact_type="engagement"):
    result = repo.search(_dict_user(user_id), type=fact_type)
    assert len(result["subjects"]) == 1
    return result["subjects"][0]


def test_projection_latest_document_date_wins(pg_env, repo):
    """Two dated, differing-value claims: the LATEST document_date wins,
    with no conflict marker."""
    import datetime as dt

    _seed_full_fixture()
    fact_id = _seed_two_claims(
        repo,
        dates=[dt.date(2026, 1, 1), dt.date(2026, 6, 1)],
        values=["planning", "active"],
    )

    from src.repositories import users_repo

    users_repo().create(id="dan", email="dan@test.com", name="Dan")
    _make_group_with_grant(pg_env, group_name="group-proj1", collection_id=CORPUS_A, member_user_id="dan")

    subject = _search_one(repo, "dan")
    assert subject["id"] == fact_id
    assert subject["attrs"]["status"] == {"value": "active", "document_date": "2026-06-01"}


def test_projection_same_date_differing_values_conflict(pg_env, repo):
    """Equal dates, differing values -> conflicted marker, never a silent
    pick."""
    import datetime as dt

    _seed_full_fixture()
    same_day = dt.date(2026, 3, 1)
    fact_id = _seed_two_claims(repo, dates=[same_day, same_day], values=["red", "blue"])

    from src.repositories import users_repo

    users_repo().create(id="dan", email="dan@test.com", name="Dan")
    _make_group_with_grant(pg_env, group_name="group-proj2", collection_id=CORPUS_A, member_user_id="dan")

    subject = _search_one(repo, "dan")
    assert subject["id"] == fact_id
    proj = subject["attrs"]["status"]
    assert proj["conflicted"] is True
    assert sorted(proj["values"]) == ["blue", "red"]


def test_projection_dated_beats_undated(pg_env, repo):
    """A dated claim beats an undated one, even with a differing value."""
    import datetime as dt

    _seed_full_fixture()
    fact_id = _seed_two_claims(repo, dates=[None, dt.date(2026, 2, 1)], values=["stale-guess", "confirmed"])

    from src.repositories import users_repo

    users_repo().create(id="dan", email="dan@test.com", name="Dan")
    _make_group_with_grant(pg_env, group_name="group-proj3", collection_id=CORPUS_A, member_user_id="dan")

    subject = _search_one(repo, "dan")
    assert subject["id"] == fact_id
    assert subject["attrs"]["status"] == {"value": "confirmed", "document_date": "2026-02-01"}


def test_projection_two_undated_differing_values_conflict(pg_env, repo):
    """Two undated, differing-value claims conflict — never a silent pick
    (deliberate refinement over "null always conflicts": here it's the ONLY
    branch where that rule still applies, since no dated claim exists at
    all)."""
    _seed_full_fixture()
    fact_id = _seed_two_claims(repo, dates=[None, None], values=["east", "west"])

    from src.repositories import users_repo

    users_repo().create(id="dan", email="dan@test.com", name="Dan")
    _make_group_with_grant(pg_env, group_name="group-proj4", collection_id=CORPUS_A, member_user_id="dan")

    subject = _search_one(repo, "dan")
    assert subject["id"] == fact_id
    proj = subject["attrs"]["status"]
    assert proj["conflicted"] is True
    assert sorted(proj["values"]) == ["east", "west"]


# ---------------------------------------------------------------------------
# all_evidence mode flip.
# ---------------------------------------------------------------------------


def _force_all_evidence_mode(monkeypatch):
    monkeypatch.setenv("AGNES_FACTS_VISIBILITY_MODE", "all_evidence")


def test_all_evidence_mode_hides_strictly_more(pg_env, repo, monkeypatch):
    """A subject with one readable + one unreadable claim is visible under
    the default `any_evidence` mode, and invisible under `all_evidence`."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Readable.")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="sha1", quote="Unreadable.")

    from src.repositories import users_repo

    users_repo().create(id="erin", email="erin@test.com", name="Erin")
    _make_group_with_grant(pg_env, group_name="group-ae", collection_id=CORPUS_A, member_user_id="erin")

    any_evidence_result = repo.search(_dict_user("erin"), type="engagement")
    assert fact_id in {s["id"] for s in any_evidence_result["subjects"]}

    _force_all_evidence_mode(monkeypatch)
    all_evidence_result = repo.search(_dict_user("erin"), type="engagement")
    assert fact_id not in {s["id"] for s in all_evidence_result["subjects"]}


# ---------------------------------------------------------------------------
# neighbors truncation flags.
# ---------------------------------------------------------------------------


def test_neighbors_fanout_truncation(pg_env, repo):
    """A hub node with more visible edges than `fanout` sets
    truncated.fanout and caps the returned edges at `fanout`."""
    _seed_full_fixture()
    hub = repo.create_fact(type="person")
    repo.add_claim(fact_id=hub, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Hub.")
    leaves = []
    for i in range(4):
        leaf = repo.create_fact(type="person")
        repo.add_claim(fact_id=leaf, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote=f"Leaf {i}.")
        edge_id = repo.create_edge(src=hub, type="knows", dst=leaf)
        repo.add_claim(
            edge_id=edge_id,
            corpus_file_id="cf_a1",
            corpus_id=CORPUS_A,
            file_sha256="sha1",
            quote=f"Hub knows leaf {i}.",
        )
        leaves.append(leaf)

    from src.repositories import users_repo

    users_repo().create(id="frank", email="frank@test.com", name="Frank")
    _make_group_with_grant(pg_env, group_name="group-fanout", collection_id=CORPUS_A, member_user_id="frank")

    result = repo.neighbors(_dict_user("frank"), hub, fanout=2, limit=500)
    assert result["truncated"]["fanout"] is True
    assert len(result["edges"]) == 2


def test_neighbors_result_truncation(pg_env, repo):
    """A small `limit` truncates the combined nodes+edges result and sets
    truncated.result."""
    _seed_full_fixture()
    hub = repo.create_fact(type="person")
    repo.add_claim(fact_id=hub, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Hub.")
    for i in range(4):
        leaf = repo.create_fact(type="person")
        repo.add_claim(fact_id=leaf, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote=f"Leaf {i}.")
        edge_id = repo.create_edge(src=hub, type="knows", dst=leaf)
        repo.add_claim(
            edge_id=edge_id,
            corpus_file_id="cf_a1",
            corpus_id=CORPUS_A,
            file_sha256="sha1",
            quote=f"Hub knows leaf {i}.",
        )

    from src.repositories import users_repo

    users_repo().create(id="gina", email="gina@test.com", name="Gina")
    _make_group_with_grant(pg_env, group_name="group-result", collection_id=CORPUS_A, member_user_id="gina")

    result = repo.neighbors(_dict_user("gina"), hub, fanout=100, limit=3)
    assert result["truncated"]["result"] is True
    assert len(result["nodes"]) + len(result["edges"]) <= 3


def test_neighbors_depth_truncation_flag_true_when_graph_continues(pg_env, repo):
    """depth=1 on an A-B-C chain returns A,B and flags truncated.depth —
    there genuinely is more graph past B."""
    _seed_full_fixture()
    fact_a = repo.create_fact(type="person")
    fact_b = repo.create_fact(type="person")
    fact_c = repo.create_fact(type="person")
    for f in (fact_a, fact_b, fact_c):
        repo.add_claim(fact_id=f, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote=f"{f} exists.")
    edge_ab = repo.create_edge(src=fact_a, type="knows", dst=fact_b)
    repo.add_claim(edge_id=edge_ab, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="A knows B.")
    edge_bc = repo.create_edge(src=fact_b, type="knows", dst=fact_c)
    repo.add_claim(edge_id=edge_bc, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="B knows C.")

    from src.repositories import users_repo

    users_repo().create(id="hank", email="hank@test.com", name="Hank")
    _make_group_with_grant(pg_env, group_name="group-depth", collection_id=CORPUS_A, member_user_id="hank")

    result = repo.neighbors(_dict_user("hank"), fact_a, depth=1)
    assert {n["id"] for n in result["nodes"]} == {fact_a, fact_b}
    assert result["truncated"]["depth"] is True


def test_neighbors_depth_truncation_flag_false_when_graph_ends_exactly_at_the_cap(pg_env, repo):
    """depth=2 on the SAME A-B-C chain fully explores it — no further edges
    exist past C, so truncated.depth must be False (a leaf reached exactly
    at the depth ceiling is not "cut off")."""
    _seed_full_fixture()
    fact_a = repo.create_fact(type="person")
    fact_b = repo.create_fact(type="person")
    fact_c = repo.create_fact(type="person")
    for f in (fact_a, fact_b, fact_c):
        repo.add_claim(fact_id=f, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote=f"{f} exists.")
    edge_ab = repo.create_edge(src=fact_a, type="knows", dst=fact_b)
    repo.add_claim(edge_id=edge_ab, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="A knows B.")
    edge_bc = repo.create_edge(src=fact_b, type="knows", dst=fact_c)
    repo.add_claim(edge_id=edge_bc, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="B knows C.")

    from src.repositories import users_repo

    users_repo().create(id="hank", email="hank@test.com", name="Hank")
    _make_group_with_grant(pg_env, group_name="group-depth2", collection_id=CORPUS_A, member_user_id="hank")

    result = repo.neighbors(_dict_user("hank"), fact_a, depth=2)
    assert {n["id"] for n in result["nodes"]} == {fact_a, fact_b, fact_c}
    assert result["truncated"]["depth"] is False


# ---------------------------------------------------------------------------
# neighbors response shape (spec §12) — nodes/edges carry the SAME projected
# subject shape search() returns, not a bare {id, type, revealed}.
# ---------------------------------------------------------------------------


def test_neighbors_node_carries_the_full_subject_shape(pg_env, repo):
    """Spec §12: a neighbors node is the SAME subject shape search() returns
    — aliases, projected attrs, claim_count, quote_count — not the bare
    {id, type, revealed} of the pre-fix response. Walking the graph must not
    force a second fact_search/fact_claims round trip just to learn a node's
    own attributes."""
    _seed_full_fixture()
    fact_a = repo.create_fact(type="person")
    repo.add_alias(fact_id=fact_a, type="person", natural_key="person:alice-doe", corpus_id=CORPUS_A)
    repo.add_claim(
        fact_id=fact_a,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Alice Doe leads the engagement.",
        attrs={"role": "lead"},
    )

    from src.repositories import users_repo

    users_repo().create(id="nora", email="nora@test.com", name="Nora")
    _make_group_with_grant(pg_env, group_name="group-nora", collection_id=CORPUS_A, member_user_id="nora")

    result = repo.neighbors(_dict_user("nora"), fact_a)
    node = next(n for n in result["nodes"] if n["id"] == fact_a)
    assert node["aliases"] == ["person:alice-doe"]
    assert node["attrs"]["role"]["value"] == "lead"
    assert node["claim_count"] == 1
    assert node["quote_count"] == 1
    assert node["revealed"] is False


def test_neighbors_endpoint_only_fact_serves_empty_attrs_with_its_aliases(pg_env, repo):
    """An endpoint-only fact (visible purely via its anchoring edge's
    readable claim) still carries its OWN aliases in the walk, but attrs
    stay {} and claim_count 0 — own-claims-only, same as search()."""
    src, dst, _edge = _seed_endpoint_only_fixture(repo, readable_edge_claim=True)

    from src.repositories import users_repo

    users_repo().create(id="oscar", email="oscar@test.com", name="Oscar")
    _make_group_with_grant(pg_env, group_name="group-oscar", collection_id=CORPUS_A, member_user_id="oscar")

    result = repo.neighbors(_dict_user("oscar"), src)
    node = next(n for n in result["nodes"] if n["id"] == dst)
    assert node["attrs"] == {}
    assert node["aliases"] == ["industry:saas"]
    assert node["claim_count"] == 0
    assert node["quote_count"] == 0


def test_neighbors_s2_shaped_attrs_never_reopen_through_the_walk(pg_env, repo):
    """S2 extended to neighbors: the SAME fact with one readable
    (existence-only) claim and one unreadable claim carrying `price` must
    NOT leak `price` through a neighbors() node either — the oracle must not
    reopen through the walk just because search() closes it."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="The engagement is underway.",
        attrs={},
    )
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_b1",
        corpus_id=CORPUS_B,
        file_sha256="sha1",
        quote="The contract value is $412,000.",
        attrs={"price": 412000},
    )

    from src.repositories import users_repo

    users_repo().create(id="petra", email="petra@test.com", name="Petra")
    _make_group_with_grant(pg_env, group_name="group-petra", collection_id=CORPUS_A, member_user_id="petra")

    result = repo.neighbors(_dict_user("petra"), fact_id)
    node = next(n for n in result["nodes"] if n["id"] == fact_id)
    assert "price" not in node["attrs"]


def test_neighbors_revealed_node_serves_attrs_with_zero_quote_count(pg_env, repo):
    """A `revealed` node in the walk shows its attrs (revealed bypasses
    grants entirely per spec §4, same as search()) but quote_count is 0 —
    the caller still never sees a quote through neighbors()."""
    _seed_full_fixture()
    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="The engagement went ahead as planned.",
        attrs={"status": "active"},
    )
    repo.upsert_correction(
        subject_kind="fact",
        subject_id=fact_id,
        natural_keys={"aliases": []},
        verdict="revealed",
        reason="publicly announced",
        decided_by="admin1",
    )

    from src.repositories import users_repo

    users_repo().create(id="quinn", email="quinn@test.com", name="Quinn")
    # Quinn has NO grants at all.

    result = repo.neighbors(_dict_user("quinn"), fact_id)
    node = next(n for n in result["nodes"] if n["id"] == fact_id)
    assert node["revealed"] is True
    assert node["attrs"]["status"]["value"] == "active"
    assert node["quote_count"] == 0


def test_neighbors_edge_carries_projected_attrs(pg_env, repo):
    """Spec §12: an edge's attrs are projected the same way as a node's,
    from the edge's OWN readable claims."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")

    fact_a = repo.create_fact(type="person")
    fact_b = repo.create_fact(type="person")
    repo.add_claim(fact_id=fact_a, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="A exists.")
    repo.add_claim(fact_id=fact_b, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="B exists.")
    edge_id = repo.create_edge(src=fact_a, type="knows", dst=fact_b)
    repo.add_claim(
        edge_id=edge_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="A knows B since 2019.",
        attrs={"since": 2019},
    )

    from src.repositories import users_repo

    users_repo().create(id="rex", email="rex@test.com", name="Rex")
    _make_group_with_grant(pg_env, group_name="group-rex", collection_id=CORPUS_A, member_user_id="rex")

    result = repo.neighbors(_dict_user("rex"), fact_a)
    edge = next(e for e in result["edges"] if e["id"] == edge_id)
    assert edge["attrs"]["since"]["value"] == 2019


# ---------------------------------------------------------------------------
# statement-timeout smoke test.
# ---------------------------------------------------------------------------


def test_statement_timeout_mechanism_actually_cancels(pg_env, repo):
    """Direct proof that the exact SQL primitive `neighbors()` applies
    (`SET LOCAL statement_timeout`, spec §12) genuinely cancels a slow
    query within the same transaction — not a no-op. Deliberately isolated
    from a real `neighbors()` call: gating this on wall-clock timing of the
    actual traversal query would be a flaky test rather than a smoke test
    of the mechanism."""
    import sqlalchemy as sa
    import sqlalchemy.exc

    with repo._engine.begin() as conn:
        conn.execute(sa.text("SET LOCAL statement_timeout = 50"))
        with pytest.raises(sqlalchemy.exc.DBAPIError):
            conn.execute(sa.text("SELECT pg_sleep(1)"))


# ---------------------------------------------------------------------------
# collection-scoped summaries (spec §13.2 "Surfaces") — Library card count +
# collection-detail facts section.
# ---------------------------------------------------------------------------


def test_count_visible_facts_for_collection_is_caller_scoped(pg_env, repo):
    """Two users, different grants on the SAME collection under
    `all_evidence` mode -> different M: alice can reach every collection a
    fact is evidenced from and sees it counted, bob can reach only CORPUS_A
    and does not."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    # Fully-in-A fact: both callers should count it once they can read A.
    fact_solo = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_solo, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Solo.")
    # Spans A and B: only a caller who can read BOTH sees it under all_evidence.
    fact_spans = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_spans, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Part A.")
    repo.add_claim(fact_id=fact_spans, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="sha1", quote="Part B.")

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    users_repo().create(id="bob", email="bob@test.com", name="Bob")
    _make_group_with_grant(pg_env, group_name="group-alice", collection_id=CORPUS_A, member_user_id="alice")
    _make_group_with_grant(pg_env, group_name="group-alice-b", collection_id=CORPUS_B, member_user_id="alice")
    _make_group_with_grant(pg_env, group_name="group-bob", collection_id=CORPUS_A, member_user_id="bob")

    import pytest as _pytest  # local import keeps the monkeypatch fixture explicit

    def _force_all_evidence(mp):
        mp.setenv("AGNES_FACTS_VISIBILITY_MODE", "all_evidence")

    mp = _pytest.MonkeyPatch()
    try:
        _force_all_evidence(mp)
        alice_count = repo.count_visible_facts_for_collection(_dict_user("alice"), CORPUS_A)
        bob_count = repo.count_visible_facts_for_collection(_dict_user("bob"), CORPUS_A)
    finally:
        mp.undo()

    assert alice_count == 2  # fact_solo + fact_spans (alice can read both A and B)
    assert bob_count == 1  # fact_solo only (fact_spans has an unreadable claim in B)


def test_count_visible_facts_for_collection_zero_when_no_facts(pg_env, repo):
    _seed_full_fixture()
    from src.repositories import users_repo

    users_repo().create(id="zoe", email="zoe@test.com", name="Zoe")
    _make_group_with_grant(pg_env, group_name="group-zoe", collection_id=CORPUS_A, member_user_id="zoe")
    assert repo.count_visible_facts_for_collection(_dict_user("zoe"), CORPUS_A) == 0


# ---------------------------------------------------------------------------
# count_visible_edges_for_collections — the edge analogue added for the
# source card's pipeline-strip "edges" number (spec §13.2), added narrowly
# alongside the existing fact counter above (there was no edge equivalent).
# ---------------------------------------------------------------------------


def test_count_visible_edges_for_collections_is_caller_scoped(pg_env, repo):
    _seed_full_fixture()
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    src = repo.create_fact(type="engagement")
    dst = repo.create_fact(type="person")
    edge_id = repo.create_edge(src=src, type="owned_by", dst=dst)
    repo.add_claim(edge_id=edge_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Owned.")

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    users_repo().create(id="bob", email="bob@test.com", name="Bob")
    _make_group_with_grant(pg_env, group_name="edge-group-alice", collection_id=CORPUS_A, member_user_id="alice")
    _make_group_with_grant(pg_env, group_name="edge-group-bob", collection_id=CORPUS_B, member_user_id="bob")

    alice_counts = repo.count_visible_edges_for_collections(_dict_user("alice"), [CORPUS_A, CORPUS_B])
    bob_counts = repo.count_visible_edges_for_collections(_dict_user("bob"), [CORPUS_A, CORPUS_B])
    assert alice_counts == {CORPUS_A: 1, CORPUS_B: 0}
    assert bob_counts == {CORPUS_A: 0, CORPUS_B: 0}


def test_count_visible_edges_for_collections_admin_sees_everything(pg_env, repo):
    _seed_full_fixture()
    src = repo.create_fact(type="engagement")
    dst = repo.create_fact(type="person")
    edge_id = repo.create_edge(src=src, type="owned_by", dst=dst)
    repo.add_claim(edge_id=edge_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Owned.")

    from src.repositories import user_group_members_repo, user_groups_repo, users_repo

    users_repo().create(id="admin1", email="admin1@test.com", name="Admin")
    admin_gid = user_groups_repo().get_by_name("Admin")["id"]
    user_group_members_repo().add_member("admin1", admin_gid, source="test-fixture")

    counts = repo.count_visible_edges_for_collections(_dict_user("admin1"), [CORPUS_A])
    assert counts == {CORPUS_A: 1}


def test_count_visible_edges_for_collections_empty_input_returns_empty(pg_env, repo):
    assert repo.count_visible_edges_for_collections(_dict_user("nobody"), []) == {}


def test_count_visible_edges_for_collections_zero_when_no_edges(pg_env, repo):
    _seed_full_fixture()
    from src.repositories import users_repo

    users_repo().create(id="zoe", email="zoe@test.com", name="Zoe")
    _make_group_with_grant(pg_env, group_name="edge-group-zoe", collection_id=CORPUS_A, member_user_id="zoe")
    assert repo.count_visible_edges_for_collections(_dict_user("zoe"), [CORPUS_A]) == {CORPUS_A: 0}


def test_collection_facts_summary_type_counts_and_paged_facts(pg_env, repo):
    """type_counts + a paged fact row list (type, display name from natural
    key, claim_count, quote_count)."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")

    fact_id = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=fact_id, type="engagement", natural_key="engagement:acme-renewal", corpus_id=CORPUS_A)
    repo.add_claim(
        fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Renewal signed."
    )

    from src.repositories import users_repo

    users_repo().create(id="carla", email="carla@test.com", name="Carla")
    _make_group_with_grant(pg_env, group_name="group-carla", collection_id=CORPUS_A, member_user_id="carla")

    summary = repo.collection_facts_summary(_dict_user("carla"), CORPUS_A)
    assert summary["total"] == 1
    assert summary["type_counts"] == {"engagement": 1}
    assert len(summary["facts"]) == 1
    row = summary["facts"][0]
    assert row["id"] == fact_id
    assert row["type"] == "engagement"
    assert row["display_name"] == "engagement:acme-renewal"
    assert row["claim_count"] == 1
    assert row["quote_count"] == 1
    assert row["conflicts"] == []
    assert summary["limit_applied"] is False


def test_collection_facts_summary_hides_facts_the_caller_cannot_read(pg_env, repo):
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Hidden.")

    from src.repositories import users_repo

    users_repo().create(id="dave", email="dave@test.com", name="Dave")
    # Dave has NO grant on CORPUS_A at all.
    summary = repo.collection_facts_summary(_dict_user("dave"), CORPUS_A)
    assert summary["total"] == 0
    assert summary["facts"] == []
    assert summary["type_counts"] == {}


def test_collection_facts_summary_conflict_rendered_inline(pg_env, repo):
    """Two readable claims on one fact with differing values for the same
    attr key -> a conflict entry carrying BOTH values and their document
    names/dates (spec §13.2: "the conflict row inline ... both values +
    their document names/dates")."""
    import datetime as dt

    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a2")

    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Status is active per doc 1.",
        attrs={"status": "active"},
        document_date=dt.date(2026, 1, 1),
    )
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_a2",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Status is closed per doc 2.",
        attrs={"status": "closed"},
        document_date=dt.date(2026, 2, 1),
    )

    from src.repositories import users_repo

    users_repo().create(id="erin2", email="erin2@test.com", name="Erin2")
    _make_group_with_grant(pg_env, group_name="group-erin2", collection_id=CORPUS_A, member_user_id="erin2")

    summary = repo.collection_facts_summary(_dict_user("erin2"), CORPUS_A)
    row = summary["facts"][0]
    assert row["claim_count"] == 2
    assert len(row["conflicts"]) == 1
    conflict = row["conflicts"][0]
    assert conflict["key"] == "status"
    values = {e["value"] for e in conflict["entries"]}
    assert values == {"active", "closed"}
    docs = {e["document_name"] for e in conflict["entries"]}
    assert docs == {"cf_a1.md", "cf_a2.md"}


def test_collection_facts_summary_revealed_hides_conflict_quotes_not_attrs(pg_env, repo):
    """A revealed subject: quote_count is 0 (quotes suppressed at the
    `claims()` endpoint), but attrs/conflicts still surface — matching
    `claims()`'s own "quote blanked, everything else stays" contract."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    fact_id = repo.create_fact(type="person")
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Sensitive.",
        attrs={"role": "exec"},
    )
    repo.upsert_correction(
        subject_kind="fact",
        subject_id=fact_id,
        natural_keys={"aliases": []},
        verdict="revealed",
        reason="publicly announced",
        decided_by="admin1",
    )

    from src.repositories import users_repo

    users_repo().create(id="finn", email="finn@test.com", name="Finn")
    # Finn has NO grants at all — revealed serves him regardless.
    summary = repo.collection_facts_summary(_dict_user("finn"), CORPUS_A)
    assert summary["total"] == 1
    row = summary["facts"][0]
    assert row["revealed"] is True
    assert row["claim_count"] == 1
    assert row["quote_count"] == 0


def test_collection_facts_summary_review_items_possible_duplicate_of(pg_env, repo):
    """`possible_duplicate_of` edges surfaced as review-item rows naming
    both subjects — spec §7.2's entity-resolution review, §13.2's rendering."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")

    fact_a = repo.create_fact(type="person")
    repo.add_alias(fact_id=fact_a, type="person", natural_key="person:jane-doe", corpus_id=CORPUS_A)
    repo.add_claim(fact_id=fact_a, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Jane Doe.")
    fact_b = repo.create_fact(type="person")
    repo.add_alias(fact_id=fact_b, type="person", natural_key="person:j-doe", corpus_id=CORPUS_A)
    repo.add_claim(fact_id=fact_b, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="J. Doe.")
    edge_id = repo.create_edge(src=fact_a, type="possible_duplicate_of", dst=fact_b)
    repo.add_claim(
        edge_id=edge_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Possibly the same."
    )

    from src.repositories import users_repo

    users_repo().create(id="gale", email="gale@test.com", name="Gale")
    _make_group_with_grant(pg_env, group_name="group-gale", collection_id=CORPUS_A, member_user_id="gale")

    summary = repo.collection_facts_summary(_dict_user("gale"), CORPUS_A)
    assert len(summary["review_items"]) == 1
    item = summary["review_items"][0]
    assert item["edge_id"] == edge_id
    names = {item["a"]["display_name"], item["b"]["display_name"]}
    assert names == {"person:jane-doe", "person:j-doe"}


def test_collection_facts_summary_review_item_hidden_when_edge_claim_unreadable(pg_env, repo):
    """S3-equivalent for review items: the edge's ONLY claim lives in an
    unreadable collection -> not surfaced, even though both endpoints are
    independently visible."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_A, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_B, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")

    fact_a = repo.create_fact(type="person")
    repo.add_claim(fact_id=fact_a, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="A.")
    fact_b = repo.create_fact(type="person")
    repo.add_claim(fact_id=fact_b, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="B.")
    edge_id = repo.create_edge(src=fact_a, type="possible_duplicate_of", dst=fact_b)
    repo.add_claim(edge_id=edge_id, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="sha1", quote="Maybe.")

    from src.repositories import users_repo

    users_repo().create(id="hana", email="hana@test.com", name="Hana")
    _make_group_with_grant(pg_env, group_name="group-hana", collection_id=CORPUS_A, member_user_id="hana")

    summary = repo.collection_facts_summary(_dict_user("hana"), CORPUS_A)
    assert summary["review_items"] == []


def test_collection_facts_summary_pagination(pg_env, repo):
    _seed_full_fixture()
    for i in range(5):
        fid = repo.create_fact(type="engagement")
        repo.add_claim(fact_id=fid, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote=f"claim {i}")

    from src.repositories import users_repo

    users_repo().create(id="ivy", email="ivy@test.com", name="Ivy")
    _make_group_with_grant(pg_env, group_name="group-ivy", collection_id=CORPUS_A, member_user_id="ivy")

    page1 = repo.collection_facts_summary(_dict_user("ivy"), CORPUS_A, limit=2, offset=0)
    assert len(page1["facts"]) == 2
    assert page1["limit_applied"] is True
    assert page1["total"] == 5

    page2 = repo.collection_facts_summary(_dict_user("ivy"), CORPUS_A, limit=2, offset=2)
    assert len(page2["facts"]) == 2
    ids_page1 = {f["id"] for f in page1["facts"]}
    ids_page2 = {f["id"] for f in page2["facts"]}
    assert ids_page1.isdisjoint(ids_page2)


def test_neighbors_applies_a_statement_timeout(pg_env, repo):
    """`neighbors()` runs cleanly under its real, non-degenerate timeout —
    the companion positive case to the cancellation test above, so the
    mechanism is proven to fire only when it should."""
    _seed_full_fixture()
    fact_id = repo.create_fact(type="person")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Exists.")

    from src.repositories import users_repo

    users_repo().create(id="ivan", email="ivan@test.com", name="Ivan")
    _make_group_with_grant(pg_env, group_name="group-timeout", collection_id=CORPUS_A, member_user_id="ivan")

    result = repo.neighbors(_dict_user("ivan"), fact_id)
    assert result["nodes"][0]["id"] == fact_id


# ---------------------------------------------------------------------------
# Type map — the Library's node-type counts must obey the same gate as
# search(), or the aggregate becomes the S1/S2 existence oracle in another
# shape: a reader counting subjects they are not allowed to read.
# ---------------------------------------------------------------------------


def test_type_map_omits_a_type_the_caller_cannot_see(pg_env, repo):
    """A type whose every subject sits behind an ungranted collection is
    ABSENT from the map — not reported with a count of 0, which would
    itself confirm the type exists and that something occupies it."""
    _seed_full_fixture()
    fact_id = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=fact_id, type="engagement", natural_key="engagement:secret-project")
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Secret Project kicked off in March.",
        attrs={"status": "active"},
    )

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(
        pg_env, group_name="group-b", collection_id="col_other_never_granted", member_user_id="alice"
    )

    assert repo.count_visible_facts_by_type(_dict_user("alice")) == {}


def test_type_map_counts_what_the_caller_can_see(pg_env, repo):
    """The uploader reaches their own collection, so the type appears with
    a real count — proving the empty result above is the grant talking and
    not the query simply never returning anything."""
    _seed_full_fixture()
    for slug in ("alpha", "beta"):
        fact_id = repo.create_fact(type="engagement")
        repo.add_alias(fact_id=fact_id, type="engagement", natural_key=f"engagement:{slug}")
        repo.add_claim(
            fact_id=fact_id,
            corpus_file_id="cf_a1",
            corpus_id=CORPUS_A,
            file_sha256="sha1",
            quote=f"{slug} kicked off in March.",
            attrs={"status": "active"},
        )
    client_id = repo.create_fact(type="client")
    repo.add_alias(fact_id=client_id, type="client", natural_key="client:parts-authority")
    repo.add_claim(
        fact_id=client_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Parts Authority signed.",
        attrs={},
    )

    assert repo.count_visible_facts_by_type(_dict_user("uploader1")) == {"client": 1, "engagement": 2}


def test_type_map_agrees_with_search_for_the_same_caller(pg_env, repo):
    """The map's number for a type is exactly what search(type=...) lets
    the same caller reach — the contract the Knowledge tab relies on when
    it makes each type a way in."""
    _seed_full_fixture()
    for slug in ("alpha", "beta"):
        fact_id = repo.create_fact(type="engagement")
        repo.add_alias(fact_id=fact_id, type="engagement", natural_key=f"engagement:{slug}")
        repo.add_claim(
            fact_id=fact_id,
            corpus_file_id="cf_a1",
            corpus_id=CORPUS_A,
            file_sha256="sha1",
            quote=f"{slug} kicked off in March.",
            attrs={},
        )

    caller = _dict_user("uploader1")
    mapped = repo.count_visible_facts_by_type(caller)
    searched = repo.search(caller, type="engagement")
    assert mapped["engagement"] == len(searched["subjects"])


# ---------------------------------------------------------------------------
# Entity facets — the Library's filter menu (TCRD-250 piece 4). Same gate as
# search(), plus one extra conservatism: the DOCUMENT tally must not report
# files sitting in a collection the caller cannot open.
# ---------------------------------------------------------------------------


def test_facets_omit_a_type_the_caller_cannot_see(pg_env, repo):
    _seed_full_fixture()
    fact_id = repo.create_fact(type="client")
    repo.add_alias(fact_id=fact_id, type="client", natural_key="client:parts-authority")
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="Parts Authority signed.",
        attrs={},
    )

    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _make_group_with_grant(
        pg_env, group_name="group-b", collection_id="col_other_never_granted", member_user_id="alice"
    )

    assert repo.facet_values(_dict_user("alice"), types=["client"]) == {"client": []}


def test_facets_carry_a_label_and_a_document_count(pg_env, repo):
    _seed_full_fixture()
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a2", sha256="sha2")
    fact_id = repo.create_fact(type="client")
    # corpus_id is the alias's provenance. Without it the alias stays
    # admin-only-visible by design (add_alias's own docstring), so a facet
    # would fall back to the opaque id — see the test below.
    repo.add_alias(
        fact_id=fact_id, type="client", natural_key="client:parts-authority", corpus_id=CORPUS_A
    )
    for file_id, sha in (("cf_a1", "sha1"), ("cf_a2", "sha2")):
        repo.add_claim(
            fact_id=fact_id,
            corpus_file_id=file_id,
            corpus_id=CORPUS_A,
            file_sha256=sha,
            quote=f"Parts Authority appears in {file_id}.",
            attrs={},
        )

    out = repo.facet_values(_dict_user("uploader1"), types=["client"])
    assert len(out["client"]) == 1
    row = out["client"][0]
    assert row["subject_id"] == fact_id
    assert row["label"] == "client:parts-authority"
    assert row["document_count"] == 2, "two files evidence this client"


def test_a_requested_type_with_nothing_in_it_returns_an_empty_list_not_a_missing_key(pg_env, repo):
    """The filter menu renders a section per requested type; a missing key
    would make 'nothing here' indistinguishable from 'never asked'."""
    _seed_full_fixture()
    out = repo.facet_values(_dict_user("uploader1"), types=["client", "industry"])
    assert set(out) == {"client", "industry"}
    assert out["client"] == [] and out["industry"] == []


def test_no_facet_types_requested_is_an_empty_result_not_a_full_scan(pg_env, repo):
    _seed_full_fixture()
    assert repo.facet_values(_dict_user("uploader1"), types=[]) == {}


def test_an_unattributed_alias_falls_back_to_the_opaque_id(pg_env, repo):
    """An alias with no recorded corpus is admin-only-visible — the same rule
    `search()` applies. A facet must then show the id rather than invent a
    label, because the label itself is evidence the caller cannot read."""
    _seed_full_fixture()
    fact_id = repo.create_fact(type="client")
    repo.add_alias(fact_id=fact_id, type="client", natural_key="client:unattributed")
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id="cf_a1",
        corpus_id=CORPUS_A,
        file_sha256="sha1",
        quote="An unattributed client appears here.",
        attrs={},
    )

    (row,) = repo.facet_values(_dict_user("uploader1"), types=["client"])["client"]
    assert row["label"] == fact_id, "no readable alias — must not leak the natural key"
    assert row["document_count"] == 1
