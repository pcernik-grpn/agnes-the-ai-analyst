"""MCP-surface RBAC parity for `fact_search`/`fact_neighbors`/`fact_claims`
(build order step 6 of
docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md).

Verifies the plumbing ABOVE the repository — token -> `_facts_caller` ->
tool call -> `facts_repo()` — resolves a restricted `AgentPrincipal` the
SAME way the REST layer's `get_current_user` dependency would, and passes
it straight through without ever substituting the owner's full authority.
The repository itself already carries the full S5 acceptance test (spec
§15.1) directly against `FactsPgRepository` in
``tests/db_pg/test_facts_read_pg.py``; this file is S5, one hop further
out, through the MCP tool functions a real agent session actually calls.

PG-only, no DuckDB half to parametrize against (A3 ratchet, `facts_pg.py`
has no DuckDB sibling).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="mcp package not installed")

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_A = "col_mcp_a"
CORPUS_B = "col_mcp_b"


@pytest.fixture
def pg_env(tmp_path, monkeypatch, pg_engine):
    """Alembic-upgraded Postgres wired as the active backend (mirrors
    ``tests/db_pg/test_facts_read_pg.py::pg_env``), with the `facts` flag on
    — the MCP tools call `require_facts_enabled()` themselves, same as the
    REST router-level dependency."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
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


@pytest.fixture
def mcp_call(pg_env):
    """`(tool_name, token, **kwargs) -> result` — sets the SSE transport's
    per-request token contextvar and calls the tool function directly (no
    ASGITransport needed: these tools never self-call over HTTP)."""
    import app.api.mcp_http as mcp_mod

    def call(name: str, token: str, **kwargs):
        tok = mcp_mod._current_token.set(token)
        try:
            fn = getattr(mcp_mod, name)
            return asyncio.run(fn(**kwargs))
        finally:
            mcp_mod._current_token.reset(tok)

    return call


def _seed_collection(*, collection_id: str, created_by: str) -> str:
    import sqlalchemy as sa

    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :slug, :name, :by)"),
            {"id": collection_id, "slug": collection_id, "name": collection_id, "by": created_by},
        )
    return collection_id


def _seed_corpus_file(*, corpus_id: str, file_id: str, sha256: str = "sha1") -> None:
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


def _make_owner_with_grants(collection_ids) -> "tuple[str, str]":
    """Owner user granted EVERY collection via a group — the ceiling the
    agent's OWN scope must narrow below (the owner is not restricted)."""
    from src.repositories import resource_grants_repo, user_group_members_repo, user_groups_repo, users_repo

    tag = uuid.uuid4().hex[:8]
    user_id, email = f"facts_mcp_owner_{tag}", f"facts_mcp_owner_{tag}@test.com"
    users_repo().create(id=user_id, email=email, name="Owner")
    grp = user_groups_repo().create(name=f"facts-mcp-grp-{tag}", description="test", created_by="test-fixture")
    user_group_members_repo().add_member(user_id, grp["id"], source="admin", added_by="test-fixture")
    for cid in collection_ids:
        resource_grants_repo().create(grp["id"], "collection", cid, "test-fixture", "required")
    return user_id, email


def _agent_session_token(owner_user_id: str, owner_email: str, *, scope) -> str:
    """Mint a real, resolvable `agent_session` JWT — mirrors
    `tests/test_agent_scope_mcp.py::_agent_session_token`, narrowed on
    `tables_mode` (the axis `collection` scoping lives under — see
    `TABLES_MODE_EXTRA_TYPES` in `src/agent_scope_intersection.py`)."""
    from app.auth.access import mint_agent_session_jwt
    from app.chat.types import Surface
    from src.repositories import agents_repo, chat_session_repo

    agent_id = str(uuid.uuid4())
    agents_repo().create(
        id=agent_id,
        owner_user_id=owner_user_id,
        name="Scoped Agent",
        slug=f"facts-scoped-agent-{uuid.uuid4().hex[:8]}",
        plugins_mode="all",
        connections_mode="all",
        tables_mode="selected",
        memory_mode="all",
    )
    agents_repo().set_scope(agent_id, list(scope))
    session = chat_session_repo().create_session(user_email=owner_email, surface=Surface.WEB, agent_id=agent_id)
    return mint_agent_session_jwt(session.id)


def _seed_two_collection_fixture():
    owner_id, owner_email = _make_owner_with_grants([CORPUS_A, CORPUS_B])
    _seed_collection(collection_id=CORPUS_A, created_by=owner_id)
    _seed_collection(collection_id=CORPUS_B, created_by=owner_id)
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_a1")
    _seed_corpus_file(corpus_id=CORPUS_B, file_id="cf_b1")
    return owner_id, owner_email


def test_s5_mcp_fact_search_narrows_to_agent_scope(pg_env, repo, mcp_call):
    """S5 (fact_search, MCP hop). The agent can reach only CORPUS_A even
    though its owner can reach both — the narrowing must survive the
    token -> AgentPrincipal -> facts_repo() plumbing, not just the repo
    call site tested directly in test_facts_read_pg.py."""
    owner_id, owner_email = _seed_two_collection_fixture()

    fact_a = repo.create_fact(type="engagement")
    fact_b = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_a, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="A.")
    repo.add_claim(fact_id=fact_b, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="sha1", quote="B.")

    token = _agent_session_token(owner_id, owner_email, scope=[("collection", CORPUS_A)])

    result = mcp_call("fact_search", token, type="engagement")
    ids = {s["id"] for s in result["subjects"]}
    assert ids == {fact_a}


def test_s5_mcp_fact_neighbors_narrows_to_agent_scope(pg_env, repo, mcp_call):
    """S5 (fact_neighbors, MCP hop). An edge whose only claim lives in the
    collection outside the agent's scope must not surface."""
    owner_id, owner_email = _seed_two_collection_fixture()

    fact_a = repo.create_fact(type="person")
    fact_b = repo.create_fact(type="person")
    repo.add_claim(fact_id=fact_a, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="A.")
    repo.add_claim(fact_id=fact_b, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="B.")
    edge_id = repo.create_edge(src=fact_a, type="knows", dst=fact_b)
    repo.add_claim(edge_id=edge_id, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="sha1", quote="A knows B.")

    token = _agent_session_token(owner_id, owner_email, scope=[("collection", CORPUS_A)])

    result = mcp_call("fact_neighbors", token, subject_id=fact_a)
    assert result["edges"] == []


def test_s5_mcp_fact_claims_narrows_to_agent_scope(pg_env, repo, mcp_call):
    """S5 (fact_claims, MCP hop). A subject visible only through a claim
    outside the agent's scope raises the SAME 404-shaped error as a
    nonexistent id — surfaced as a `ValueError` carrying the shared hint,
    never a bare empty/partial list."""
    owner_id, owner_email = _seed_two_collection_fixture()

    fact_id = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_id, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="sha1", quote="B.")

    token = _agent_session_token(owner_id, owner_email, scope=[("collection", CORPUS_A)])

    with pytest.raises(ValueError) as excinfo:
        mcp_call("fact_claims", token, subject_id=fact_id)
    assert "the id is wrong" in str(excinfo.value)


def test_owner_direct_call_sees_the_full_grant(pg_env, repo, mcp_call):
    """Positive control: the OWNER's own session token (a plain dict user,
    not a restricted principal) sees both collections — the narrowing
    above is the agent's scope, not a blanket lockout."""
    from app.auth.jwt import create_access_token

    owner_id, owner_email = _seed_two_collection_fixture()

    fact_a = repo.create_fact(type="engagement")
    fact_b = repo.create_fact(type="engagement")
    repo.add_claim(fact_id=fact_a, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="A.")
    repo.add_claim(fact_id=fact_b, corpus_file_id="cf_b1", corpus_id=CORPUS_B, file_sha256="sha1", quote="B.")

    token = create_access_token(user_id=owner_id, email=owner_email)
    result = mcp_call("fact_search", token, type="engagement")
    ids = {s["id"] for s in result["subjects"]}
    assert ids == {fact_a, fact_b}


def test_fact_search_q_reaches_the_repository(pg_env, repo, mcp_call):
    """`q` on the `fact_search` MCP tool reaches `FactsPgRepository.search`
    (not silently dropped) -- the same free-text alias lookup as
    `POST /api/facts/search` and `agnes facts search <type> [QUERY]`."""
    from app.auth.jwt import create_access_token

    owner_id, owner_email = _seed_two_collection_fixture()

    parts_authority = repo.create_fact(type="organization")
    repo.add_alias(
        fact_id=parts_authority, type="organization", natural_key="organization:parts-authority", corpus_id=CORPUS_A
    )
    repo.add_claim(fact_id=parts_authority, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="PA.")
    other = repo.create_fact(type="organization")
    repo.add_alias(fact_id=other, type="organization", natural_key="organization:zephyr-corp", corpus_id=CORPUS_A)
    repo.add_claim(fact_id=other, corpus_file_id="cf_a1", corpus_id=CORPUS_A, file_sha256="sha1", quote="Zephyr.")

    token = create_access_token(user_id=owner_id, email=owner_email)
    result = mcp_call("fact_search", token, type="organization", q="Parts Authority")
    ids = [s["id"] for s in result["subjects"]]
    assert ids == [parts_authority]
