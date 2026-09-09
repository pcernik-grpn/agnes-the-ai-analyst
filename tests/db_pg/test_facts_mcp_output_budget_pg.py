"""MCP output-budget disclosure for the fact-graph tools (`fact_edges`,
`fact_neighbors`, `fact_claims`) — measured on a production instance
(2026-09): a well-connected node's edges/claims/quotes routinely blew the
agent sandbox's tool output cap even WITH an explicit `limit` — `limit`
bounds COUNT, not serialized SIZE.

Mirrors the fixtures in `tests/db_pg/test_facts_mcp_pg.py` (pg_env / repo /
mcp_call) — PG-only, no DuckDB half to parametrize against (`facts_pg.py`
has no DuckDB sibling, A3 ratchet).
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="mcp package not installed")

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_A = "col_budget_a"


@pytest.fixture
def pg_env(tmp_path, monkeypatch, pg_engine):
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
    from src import db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from tests.db_pg._parity_sweep_util import _seed_pg_system_groups

    _seed_pg_system_groups(pg_engine)
    return pg_engine


@pytest.fixture
def repo(pg_env):
    from src import db_pg
    from src.repositories.facts_pg import FactsPgRepository

    return FactsPgRepository(db_pg.get_engine())


@pytest.fixture
def mcp_call(pg_env):
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


def _seed_corpus_file(*, corpus_id: str, file_id: str) -> None:
    import sqlalchemy as sa

    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO corpus_files (id, corpus_id, filename, sha256) "
                "VALUES (:id, :corpus_id, :filename, :sha256)"
            ),
            {"id": file_id, "corpus_id": corpus_id, "filename": f"{file_id}.md", "sha256": "sha1"},
        )


def _make_owner_with_grant(collection_id: str) -> tuple[str, str]:
    from src.repositories import resource_grants_repo, user_group_members_repo, user_groups_repo, users_repo

    tag = uuid.uuid4().hex[:8]
    user_id, email = f"facts_budget_owner_{tag}", f"facts_budget_owner_{tag}@test.com"
    users_repo().create(id=user_id, email=email, name="Owner")
    grp = user_groups_repo().create(name=f"facts-budget-grp-{tag}", description="test", created_by="test-fixture")
    user_group_members_repo().add_member(user_id, grp["id"], source="admin", added_by="test-fixture")
    resource_grants_repo().create(grp["id"], "collection", collection_id, "test-fixture", "required")
    return user_id, email


def _token(owner_id: str, owner_email: str) -> str:
    from app.auth.jwt import create_access_token

    return create_access_token(user_id=owner_id, email=owner_email)


def _seed_many_edges(repo, *, n: int, quote_len: int = 0) -> str:
    """One collection, ``n`` distinct ``knows`` edges between fresh fact
    pairs, each backed by a readable claim — optionally a long one, to make
    ``include_claims=1`` blow the output budget on its own."""
    owner_id, owner_email = _make_owner_with_grant(CORPUS_A)
    _seed_collection(collection_id=CORPUS_A, created_by=owner_id)
    _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_budget_a")

    for i in range(n):
        src = repo.create_fact(type="person")
        dst = repo.create_fact(type="person")
        edge_id = repo.create_edge(src=src, type="knows", dst=dst)
        repo.add_claim(
            edge_id=edge_id,
            corpus_file_id="cf_budget_a",
            corpus_id=CORPUS_A,
            file_sha256="sha1",
            quote=f"edge {i} connects them.",
        )
        if quote_len:
            repo.add_claim(
                edge_id=edge_id,
                corpus_file_id="cf_budget_a",
                corpus_id=CORPUS_A,
                file_sha256="sha1",
                quote=("Evidence text for this relationship. " * 200)[:quote_len],
            )
    return _token(owner_id, owner_email)


class TestFactEdgesOutputBudget:
    def test_oversized_result_is_compacted_with_disclosure(self, repo, mcp_call, monkeypatch):
        from src.mcp_tooling import DEFAULT_SEARCH_MAX_CHARS, wire_size

        token = _seed_many_edges(repo, n=80, quote_len=1_800)

        # Confirm the fixture actually reproduces an oversized result — the
        # tool compacts by DEFAULT (`AGNES_MCP_SEARCH_MAX_CHARS` unset still
        # applies DEFAULT_SEARCH_MAX_CHARS), so measure the raw size with
        # compaction disabled first.
        monkeypatch.setenv("AGNES_MCP_SEARCH_MAX_CHARS", "0")
        raw = mcp_call("fact_edges", token, edge_type="knows", limit=80, include_claims=1)
        assert wire_size(raw) > DEFAULT_SEARCH_MAX_CHARS
        assert "output" not in raw["truncated"] or raw["truncated"].get("output") is not True

        monkeypatch.setenv("AGNES_MCP_SEARCH_MAX_CHARS", str(DEFAULT_SEARCH_MAX_CHARS))
        out = mcp_call("fact_edges", token, edge_type="knows", limit=80, include_claims=1)
        assert wire_size(out) <= DEFAULT_SEARCH_MAX_CHARS
        assert out["truncated"]["output"] is True
        assert "truncated_note" in out
        # The pre-existing structured flags are never replaced by a bare bool.
        assert out["truncated"]["result"] is False

    def test_nodes_never_dangle_after_edges_are_dropped(self, repo, mcp_call, monkeypatch):
        token = _seed_many_edges(repo, n=100, quote_len=2_500)
        monkeypatch.setenv("AGNES_MCP_SEARCH_MAX_CHARS", "4000")
        out = mcp_call("fact_edges", token, edge_type="knows", limit=100, include_claims=1)
        referenced = {e["src"] for e in out["edges"]} | {e["dst"] for e in out["edges"]}
        assert {n["id"] for n in out["nodes"]} == referenced

    def test_small_result_is_untouched(self, repo, mcp_call):
        token = _seed_many_edges(repo, n=1)
        out = mcp_call("fact_edges", token, edge_type="knows", limit=10)
        assert "output" not in out["truncated"] or out["truncated"].get("output") is not True


class TestFactNeighborsOutputBudget:
    def test_oversized_traversal_is_compacted_with_disclosure(self, repo, mcp_call, monkeypatch):
        """`fact_neighbors` fans out from ONE root, so seed a hub connected
        to many distinct people, each edge carrying a long inline claim."""
        owner_id, owner_email = _make_owner_with_grant(CORPUS_A)
        _seed_collection(collection_id=CORPUS_A, created_by=owner_id)
        _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_budget_a")
        token = _token(owner_id, owner_email)

        hub = repo.create_fact(type="person")
        repo.add_claim(fact_id=hub, corpus_file_id="cf_budget_a", corpus_id=CORPUS_A, file_sha256="sha1", quote="hub.")
        for i in range(80):
            leaf = repo.create_fact(type="person")
            repo.add_claim(
                fact_id=leaf, corpus_file_id="cf_budget_a", corpus_id=CORPUS_A, file_sha256="sha1", quote=f"leaf {i}."
            )
            edge_id = repo.create_edge(src=hub, type="knows", dst=leaf)
            repo.add_claim(
                edge_id=edge_id,
                corpus_file_id="cf_budget_a",
                corpus_id=CORPUS_A,
                file_sha256="sha1",
                quote=("Evidence text for this relationship. " * 200)[:1_800],
            )

        from src.mcp_tooling import DEFAULT_SEARCH_MAX_CHARS, wire_size

        monkeypatch.setenv("AGNES_MCP_SEARCH_MAX_CHARS", str(DEFAULT_SEARCH_MAX_CHARS))
        out = mcp_call("fact_neighbors", token, subject_id=hub, limit=500, fanout=100, include_claims=1)
        assert wire_size(out) <= DEFAULT_SEARCH_MAX_CHARS
        assert out["truncated"]["output"] is True
        assert isinstance(out["truncated"], dict)
        assert "depth" in out["truncated"] and "fanout" in out["truncated"]


class TestFactClaimsOutputBudget:
    def test_oversized_claims_are_shortened_then_dropped_with_disclosure(self, repo, mcp_call, monkeypatch):
        from src.mcp_tooling import DEFAULT_SEARCH_MAX_CHARS, wire_size

        owner_id, owner_email = _make_owner_with_grant(CORPUS_A)
        _seed_collection(collection_id=CORPUS_A, created_by=owner_id)
        _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_budget_a")
        token = _token(owner_id, owner_email)

        subject = repo.create_fact(type="engagement")
        for i in range(120):
            repo.add_claim(
                fact_id=subject,
                corpus_file_id="cf_budget_a",
                corpus_id=CORPUS_A,
                file_sha256="sha1",
                quote=(f"claim {i}: " + "evidence text. " * 100),
            )

        monkeypatch.setenv("AGNES_MCP_SEARCH_MAX_CHARS", str(DEFAULT_SEARCH_MAX_CHARS))
        out = mcp_call("fact_claims", token, subject_id=subject, limit=120)
        assert wire_size(out) <= DEFAULT_SEARCH_MAX_CHARS
        assert out["truncated"] is True
        assert "limit=" in out["truncated_note"]

    def test_small_result_is_untouched(self, repo, mcp_call):
        owner_id, owner_email = _make_owner_with_grant(CORPUS_A)
        _seed_collection(collection_id=CORPUS_A, created_by=owner_id)
        _seed_corpus_file(corpus_id=CORPUS_A, file_id="cf_budget_a")
        token = _token(owner_id, owner_email)
        subject = repo.create_fact(type="engagement")
        repo.add_claim(fact_id=subject, corpus_file_id="cf_budget_a", corpus_id=CORPUS_A, file_sha256="sha1", quote="x")

        out = mcp_call("fact_claims", token, subject_id=subject)
        assert "truncated" not in out
