"""``claims.audience`` — Slice 4b (2026-08-30 sharepoint-acl-mirroring plan,
Task 10; spec ``2026-08-28-sharepoint-acl-mirroring-design.md`` §4.2-§4.4):
index-time audience-variant tagging composed with collection-grant
reachability in ``src.repositories.facts_pg``.

PG-only, no DuckDB half to parametrize against (A3 ratchet, and ``claims``
carries no DuckDB sibling at all — see ``migrations/versions/
0076_facts_tables.py``). Fixtures mirror ``tests/db_pg/test_facts_read_pg.py``'s
seeding idiom (direct :class:`FactsPgRepository` write/seed methods, raw SQL
for ``file_corpora``/``corpus_files``); the tiered-collection config shape
mirrors ``tests/test_audience_classes.py``'s ``_seed_tiered_collection``
(a ``source_connections`` row, ``source_type='sharepoint'``,
``config.scopes[].audience_classes``) since :func:`src.audience_classes.
audience_class_map` is the only reader of that shape and does not care
whether the row backing it is real SharePoint config or a test fixture.

Every visibility assertion proves itself through a non-admin caller with a
deliberately scoped grant/class membership — never through the ``Admin``
god-mode short-circuit alone (see ``test_facts_read_pg.py``'s module
docstring for why, and ``CONTRIBUTING.md``'s "Testing conventions").
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_PLAIN = "col_plain"
CORPUS_TIERED = "col_tiered"


# ---------------------------------------------------------------------------
# fixtures / seeding helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_env(tmp_path, monkeypatch, pg_engine):
    """Alembic-upgraded Postgres wired as the active backend (mirrors
    ``test_facts_read_pg.py``'s own ``pg_env``)."""
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


def _admin_user(pg_engine) -> dict:
    """A REAL Admin-group member (``accessible_collection_ids`` resolves
    admin status via an actual membership row, per ``test_facts_read_pg.py``'s
    own ``_admin()`` precedent)."""
    from src.repositories import user_group_members_repo, users_repo

    users_repo().create(id="admin1", email="admin@test.com", name="Admin")
    with pg_engine.connect() as conn:
        admin_gid = conn.execute(sa.text("SELECT id FROM user_groups WHERE name = 'Admin'")).scalar()
    user_group_members_repo().add_member("admin1", admin_gid, source="system_seed")
    return _dict_user("admin1")


def _make_group(name: str) -> str:
    from src.repositories import user_groups_repo

    return user_groups_repo().create(name=name, description="test", created_by="test-fixture")["id"]


def _add_member(user_id: str, group_id: str) -> None:
    from src.repositories import user_group_members_repo

    user_group_members_repo().add_member(user_id, group_id, source="admin", added_by="test-fixture")


def _grant_collection(*, group_id: str, collection_id: str) -> None:
    from src.repositories import resource_grants_repo

    resource_grants_repo().create(group_id, "collection", collection_id, "test-fixture", "required")


def _seed_uploader(user_id: str) -> None:
    """Fixtures must be uploaded by an account that is NOT the probed
    caller (spec §5) — ownership unions into a dict user's readable set."""
    from src.repositories import users_repo

    users_repo().create(id=user_id, email=f"{user_id}@test.com", name=user_id)


def _seed_collection(*, collection_id: str, created_by: str) -> str:
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :slug, :name, :by)"),
            {"id": collection_id, "slug": collection_id, "name": collection_id, "by": created_by},
        )
    return collection_id


def _seed_corpus_file(
    *,
    corpus_id: str,
    file_id: str,
    sha256: str = "sha1",
    status: str = "pending",
    path: str | None = None,
) -> None:
    from src.db_pg import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO corpus_files (id, corpus_id, filename, sha256, processing_status, path) "
                "VALUES (:id, :corpus_id, :filename, :sha256, :status, :path)"
            ),
            {
                "id": file_id,
                "corpus_id": corpus_id,
                "filename": f"{file_id}.md",
                "sha256": sha256,
                "status": status,
                "path": path,
            },
        )


def _seed_tiered_collection(collection_id: str, classes: list[tuple[str, list[str]]]) -> None:
    """One SharePoint connection with a single confirmed, tiered scope — the
    exact ``config.scopes`` shape ``app/api/admin_sharepoint.py::confirm_scope``
    writes (mirrors ``tests/test_audience_classes.py``'s helper of the same
    name). ``classes`` is ordered most-privileged first."""
    from src.repositories import source_connections_repo

    source_connections_repo().create(
        id=uuid.uuid4().hex,
        name=f"conn-{collection_id}",
        source_type="sharepoint",
        config={
            "scopes": [
                {
                    "source_scope_id": f"drive:{collection_id}",
                    "display_path": "Aud",
                    "collection_id": collection_id,
                    "audience_classes": [{"name": name, "group_ids": gids} for name, gids in classes],
                }
            ]
        },
    )


def _seed_matrix(pg_engine, repo):
    """The Task 10 fixture, shared by every visibility-matrix test below:

    - ``col_plain`` (non-tiered): one untagged claim (``fact_plain``) —
      regression control, must read identically to pre-audience behavior.
    - ``col_tiered`` (tiered, classes ``full`` > ``redacted``):
        - ``fact_untagged``/``cf_untagged``: one untagged claim — probes the
          must_not/should_not fork on its own, independent of the mixed pair.
        - ``fact_mixed``/``cf_mixed``: ``full`` and ``redacted`` variants of
          the SAME (fact, file) pair — the spec's canonical "$20k vs
          <redacted>" shape.

    Returns ``(full_group_id, reader_group_id, fact_plain, fact_untagged,
    fact_mixed)`` — the caller adds probed users to ``reader_group_id``
    (collection reachability) and, for a "top class" caller, also to
    ``full_group_id`` (audience-class membership) — two independent group
    memberships, matching the design's two-layer composition (spec §4.2).
    """
    _seed_uploader("uploader1")

    _seed_collection(collection_id=CORPUS_PLAIN, created_by="uploader1")
    _seed_collection(collection_id=CORPUS_TIERED, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_PLAIN, file_id="cf_plain")
    _seed_corpus_file(corpus_id=CORPUS_TIERED, file_id="cf_untagged")
    _seed_corpus_file(corpus_id=CORPUS_TIERED, file_id="cf_mixed")

    full_gid = _make_group("audience-full")
    redacted_gid = _make_group("audience-redacted")
    reader_gid = _make_group("collection-readers")
    _seed_tiered_collection(CORPUS_TIERED, [("full", [full_gid]), ("redacted", [redacted_gid])])

    _grant_collection(group_id=reader_gid, collection_id=CORPUS_PLAIN)
    _grant_collection(group_id=reader_gid, collection_id=CORPUS_TIERED)

    fact_plain = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=fact_plain, type="engagement", natural_key="engagement:plain-target")
    repo.add_claim(
        fact_id=fact_plain,
        corpus_file_id="cf_plain",
        corpus_id=CORPUS_PLAIN,
        file_sha256="sha1",
        quote="Plain evidence, unrestricted.",
        attrs={"status": "active"},
    )

    fact_untagged = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=fact_untagged, type="engagement", natural_key="engagement:untagged-in-tiered")
    repo.add_claim(
        fact_id=fact_untagged,
        corpus_file_id="cf_untagged",
        corpus_id=CORPUS_TIERED,
        file_sha256="sha1",
        quote="Untagged evidence in a tiered scope.",
        attrs={"status": "active"},
    )

    fact_mixed = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=fact_mixed, type="engagement", natural_key="engagement:mixed-target")
    repo.add_claim(
        fact_id=fact_mixed,
        corpus_file_id="cf_mixed",
        corpus_id=CORPUS_TIERED,
        file_sha256="sha1",
        quote="The contract value is $20k.",
        attrs={"budget": "$20k"},
        audience="full",
    )
    repo.add_claim(
        fact_id=fact_mixed,
        corpus_file_id="cf_mixed",
        corpus_id=CORPUS_TIERED,
        file_sha256="sha1",
        quote="The contract value is <redacted>.",
        attrs={"budget": "<redacted>"},
        audience="redacted",
    )

    return full_gid, reader_gid, fact_plain, fact_untagged, fact_mixed


# ---------------------------------------------------------------------------
# visibility matrix (plan Task 10, Step 1)
# ---------------------------------------------------------------------------


def test_plain_collection_is_unchanged(pg_env, repo):
    """Regression: a non-tiered collection's untagged claim is unaffected by
    the audience predicate — every caller who could see it before still can."""
    full_gid, reader_gid, fact_plain, fact_untagged, fact_mixed = _seed_matrix(pg_env, repo)
    from src.repositories import users_repo

    users_repo().create(id="alice", email="alice@test.com", name="Alice")
    _add_member("alice", reader_gid)

    result = repo.search(_dict_user("alice"), type="engagement")
    ids = {s["id"] for s in result["subjects"]}
    assert fact_plain in ids

    claims = repo.claims(_dict_user("alice"), fact_plain)
    assert len(claims["claims"]) == 1
    assert claims["claims"][0]["quote"] == "Plain evidence, unrestricted."


def test_must_not_no_class_user_sees_nothing_from_tiered_scope(pg_env, repo, monkeypatch):
    """must_not (default guarantee mode): a reader with NO audience class on
    a tiered collection sees neither the untagged claim (admin-only under
    must_not — the "re-index before enable" enforcement, module docstring)
    nor either tagged variant."""
    monkeypatch.delenv("AGNES_ACL_GUARANTEE_MODE", raising=False)  # default is must_not
    full_gid, reader_gid, fact_plain, fact_untagged, fact_mixed = _seed_matrix(pg_env, repo)
    from src.repositories import users_repo

    users_repo().create(id="noclass", email="noclass@test.com", name="NoClass")
    _add_member("noclass", reader_gid)

    result = repo.search(_dict_user("noclass"), type="engagement")
    ids = {s["id"] for s in result["subjects"]}
    assert fact_untagged not in ids
    assert fact_mixed not in ids
    assert fact_plain in ids  # plain collection is untouched (regression)

    from src.repositories.facts_pg import FactNotFound

    with pytest.raises(FactNotFound):
        repo.claims(_dict_user("noclass"), fact_untagged)
    with pytest.raises(FactNotFound):
        repo.claims(_dict_user("noclass"), fact_mixed)


def test_must_not_top_class_user_sees_full_variant_only(pg_env, repo, monkeypatch):
    """must_not: a reader holding the ``full`` class sees the mixed pair's
    ``full`` variant only (dedup/predicate picks it) — never ``redacted`` —
    and still nothing from the untagged-in-tiered fact (untagged is
    admin-only under must_not regardless of the caller's own class)."""
    monkeypatch.delenv("AGNES_ACL_GUARANTEE_MODE", raising=False)
    full_gid, reader_gid, fact_plain, fact_untagged, fact_mixed = _seed_matrix(pg_env, repo)
    from src.repositories import users_repo

    users_repo().create(id="topclass", email="topclass@test.com", name="TopClass")
    _add_member("topclass", reader_gid)
    _add_member("topclass", full_gid)

    result = repo.search(_dict_user("topclass"), type="engagement")
    by_id = {s["id"]: s for s in result["subjects"]}
    assert fact_untagged not in by_id
    assert fact_mixed in by_id
    # S2 extension: attrs projection never includes a variant the caller
    # can't read — the redacted value must never appear here.
    assert by_id[fact_mixed]["attrs"]["budget"]["value"] == "$20k"

    claims = repo.claims(_dict_user("topclass"), fact_mixed)
    quotes = [c["quote"] for c in claims["claims"]]
    assert quotes == ["The contract value is $20k."]


def test_admin_sees_every_variant(pg_env, repo):
    """Admin god-mode outranks both layers (spec §4.3): every claim in the
    tiered scope is visible, dedup never applied."""
    full_gid, reader_gid, fact_plain, fact_untagged, fact_mixed = _seed_matrix(pg_env, repo)
    admin = _admin_user(pg_env)

    result = repo.search(admin, type="engagement")
    ids = {s["id"] for s in result["subjects"]}
    assert {fact_plain, fact_untagged, fact_mixed} <= ids

    claims = repo.claims(admin, fact_mixed)
    quotes = {c["quote"] for c in claims["claims"]}
    assert quotes == {"The contract value is $20k.", "The contract value is <redacted>."}


def test_should_not_untagged_is_visible_to_everyone(pg_env, repo, monkeypatch):
    """should_not (best-effort posture): the untagged-in-tiered claim stays
    unrestricted within its collection — visible even to a caller holding no
    audience class — while a caller with no class still sees no tagged
    variant of the mixed pair."""
    monkeypatch.setenv("AGNES_ACL_GUARANTEE_MODE", "should_not")
    full_gid, reader_gid, fact_plain, fact_untagged, fact_mixed = _seed_matrix(pg_env, repo)
    from src.repositories import users_repo

    users_repo().create(id="noclass2", email="noclass2@test.com", name="NoClass2")
    _add_member("noclass2", reader_gid)

    result = repo.search(_dict_user("noclass2"), type="engagement")
    ids = {s["id"] for s in result["subjects"]}
    assert fact_untagged in ids
    assert fact_mixed not in ids

    claims = repo.claims(_dict_user("noclass2"), fact_untagged)
    assert len(claims["claims"]) == 1


# ---------------------------------------------------------------------------
# ingest round-trip + format validation (plan Task 10, Step 1)
# ---------------------------------------------------------------------------


def test_ingest_round_trip_stores_audience_on_the_claim(pg_env, repo):
    """``evidence[].audience`` survives the ingest write path onto the
    claim row verbatim."""
    _seed_uploader("uploader1")
    _seed_collection(collection_id=CORPUS_TIERED, created_by="uploader1")
    _seed_corpus_file(corpus_id=CORPUS_TIERED, file_id="cf_ingest1", status="indexed", path="cf_ingest1.md")

    report = repo.ingest_batch(
        documents=[{"doc_id": "doc1", "corpus_id": CORPUS_TIERED, "path": "cf_ingest1.md"}],
        full_documents=[],
        nodes=[
            {
                "id": "engagement:ingest-target",
                "type": "engagement",
                "attrs": {"budget": "$20k"},
                "evidence": [{"doc_id": "doc1", "quote": "cf_ingest1.md", "audience": "full"}],
            }
        ],
        edges=[],
    )
    assert report["claims_written"] == 1

    with pg_env.connect() as conn:
        stored = conn.execute(sa.text("SELECT audience FROM claims WHERE corpus_file_id = 'cf_ingest1'")).scalar()
    assert stored == "full"


def test_http_ingest_rejects_invalid_audience_with_422(tmp_path, monkeypatch, pg_engine):
    """A malformed ``evidence[].audience`` (fails ``^[a-z0-9_-]{1,64}$``) is
    a whole-batch 422, nothing written — checked before any DB lookup."""
    from tests.db_pg._parity_sweep_util import build_seeded_client

    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)

    body = {
        "nodes": [
            {
                "id": "engagement:x",
                "type": "engagement",
                "attrs": {},
                "evidence": [{"doc_id": "phantom", "quote": "q", "audience": "BAD NAME"}],
            }
        ]
    }
    r = client.post("/api/facts/ingest", json=body, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["reason"] == "invalid_audience"
