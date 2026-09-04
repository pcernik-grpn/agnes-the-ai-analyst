"""v106 — PAT ``surface`` (data-read surface): column, resolver stash, RBAC gate.

The contract under test (docs/brainstorms/2026-07-28-stack-scoped-admin-surface.md v2):

- ``personal_access_tokens.surface`` exists, defaults to ``'all'`` (legacy
  grandfather is schema-level, not an app NULL sentinel);
- ``resolve_token_to_user`` stashes ``user["credential_surface"]`` from the
  token row for PAT-typed credentials;
- ``src.rbac`` admin god-mode short-circuit fires ONLY for
  admin AND surface=='all' — an admin on a ``surface='stack'`` PAT falls
  into the analyst stack branch; non-PAT credentials (no key) read as 'all'.
"""

import hashlib
import tempfile

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


# ---------------------------------------------------------------------------
# Schema + repo
# ---------------------------------------------------------------------------


def test_pat_table_has_surface_column_defaulting_all(fresh_db):
    from src.db import close_system_db, get_system_db

    conn = get_system_db()
    try:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(personal_access_tokens)").fetchall()]
        assert "surface" in cols
        # Default: a row inserted without surface reads back 'all'.
        conn.execute(
            "INSERT INTO personal_access_tokens (id, user_id, name, token_hash, prefix) "
            "VALUES ('legacy1', 'u1', 'legacy', 'h', 'p')"
        )
        val = conn.execute("SELECT surface FROM personal_access_tokens WHERE id='legacy1'").fetchone()[0]
        assert val == "all"
    finally:
        conn.close()
        close_system_db()


def test_repo_create_persists_surface(fresh_db):
    from src.db import close_system_db, get_system_db
    from src.repositories.access_tokens import AccessTokenRepository

    conn = get_system_db()
    try:
        repo = AccessTokenRepository(conn)
        repo.create(id="t-stack", user_id="u1", name="n", token_hash="h", prefix="p1", surface="stack")
        repo.create(id="t-default", user_id="u1", name="n", token_hash="h", prefix="p2")
        assert repo.get_by_id("t-stack")["surface"] == "stack"
        assert repo.get_by_id("t-default")["surface"] == "all"
    finally:
        conn.close()
        close_system_db()


# ---------------------------------------------------------------------------
# Resolver stash
# ---------------------------------------------------------------------------


def _mint(conn, user_id: str, email: str, surface: str | None) -> str:
    import uuid

    from app.auth.jwt import create_access_token
    from src.repositories.access_tokens import AccessTokenRepository

    tid = str(uuid.uuid4())
    jwt = create_access_token(user_id=user_id, email=email, token_id=tid, typ="pat", omit_exp=True)
    kwargs = {} if surface is None else {"surface": surface}
    AccessTokenRepository(conn).create(
        id=tid,
        user_id=user_id,
        name="t",
        token_hash=hashlib.sha256(jwt.encode()).hexdigest(),
        prefix=tid[:8],
        **kwargs,
    )
    return jwt


def test_resolver_stashes_credential_surface(fresh_db, monkeypatch):
    from src.db import close_system_db, get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        UserRepository(conn).create("u1", "u1@test", "U1")
        jwt_stack = _mint(conn, "u1", "u1@test", "stack")
        jwt_default = _mint(conn, "u1", "u1@test", None)

        from app.auth.pat_resolver import resolve_token_to_user

        user, reason = resolve_token_to_user(conn, jwt_stack, request=None)
        assert reason is None
        assert user["credential_surface"] == "stack"

        user, reason = resolve_token_to_user(conn, jwt_default, request=None)
        assert reason is None
        assert user["credential_surface"] == "all"
    finally:
        conn.close()
        close_system_db()


def test_session_token_carries_no_surface_key(fresh_db):
    """Non-PAT credential ⇒ no ``credential_surface`` key ⇒ reads as 'all'."""
    from src.db import close_system_db, get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        UserRepository(conn).create("u2", "u2@test", "U2")
        from app.auth.jwt import create_access_token
        from app.auth.pat_resolver import resolve_token_to_user

        session_jwt = create_access_token(user_id="u2", email="u2@test")  # typ=session
        user, reason = resolve_token_to_user(conn, session_jwt, request=None)
        assert reason is None
        assert "credential_surface" not in user
    finally:
        conn.close()
        close_system_db()


def test_agent_scoped_session_tokens_get_stack_surface(fresh_db):
    """Session JWTs minted for AGENT surfaces (chat runner scope='chat',
    MCP OAuth scope='mcp-oauth') stamp the stack surface; other scope
    values behave like a plain browser session (no key)."""
    from src.db import close_system_db, get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        UserRepository(conn).create("u3", "u3@test", "U3")
        from app.auth.jwt import create_access_token
        from app.auth.pat_resolver import resolve_token_to_user

        for scope, expect_stack in (("chat", True), ("mcp-oauth", True), ("general", False)):
            jwt = create_access_token(user_id="u3", email="u3@test", extra_claims={"scope": scope})
            user, reason = resolve_token_to_user(conn, jwt, request=None)
            assert reason is None, scope
            if expect_stack:
                assert user["credential_surface"] == "stack", scope
            else:
                assert "credential_surface" not in user, scope
    finally:
        conn.close()
        close_system_db()


def test_mcp_oauth_mints_scope_claim(fresh_db):
    """Both MCP-OAuth mint sites tag their access tokens with scope='mcp-oauth'
    so the resolver's surface stamp actually fires."""
    import inspect

    from app.auth import mcp_oauth

    src = inspect.getsource(mcp_oauth)
    assert src.count('extra_claims={"scope": "mcp-oauth"}') == 2


# ---------------------------------------------------------------------------
# RBAC gate (src/rbac.py)
# ---------------------------------------------------------------------------


def test_credential_surface_helper_fails_closed():
    from src.rbac import _credential_surface

    assert _credential_surface({}) == "all"  # no key → all
    assert _credential_surface({"credential_surface": "all"}) == "all"
    assert _credential_surface({"credential_surface": "stack"}) == "stack"
    assert _credential_surface({"credential_surface": "bogus"}) == "stack"  # unknown → narrow
    assert _credential_surface(object()) == "all"  # non-dict (Principal) → all


def _rbac_env(monkeypatch, *, admin: bool, stack_tables: list[str]):
    """Monkeypatch the collaborators of get_accessible_tables/can_access_table."""
    import app.auth.access as access_mod

    monkeypatch.setattr(access_mod, "is_user_admin", lambda uid, conn=None: admin)

    class _Entry:
        def __init__(self, id):
            self.id = id

    import app.services.stack_resolver as resolver_mod

    class _FakeResolver:
        def __init__(self, conn=None):
            pass

        def stack(self, user_id, rt):
            return [_Entry("pkg1")] if stack_tables else []

    monkeypatch.setattr(resolver_mod, "StackResolver", _FakeResolver)

    import src.repositories as repos_mod

    class _FakePkgRepo:
        def list_member_table_ids(self, pkg_ids):
            return list(stack_tables)

        def list_packages_of_table(self, table_id):
            return [{"id": "pkg1"}] if table_id in stack_tables else []

    monkeypatch.setattr(repos_mod, "data_packages_repo", lambda: _FakePkgRepo())


def test_admin_all_surface_keeps_god_mode(fresh_db, monkeypatch):
    from src.rbac import get_accessible_tables

    _rbac_env(monkeypatch, admin=True, stack_tables=["t1"])
    user = {"id": "admin1", "credential_surface": "all"}
    assert get_accessible_tables(user) is None  # None sentinel = everything


def test_admin_stack_surface_is_stack_scoped(fresh_db, monkeypatch):
    from src.rbac import can_access_table, get_accessible_tables

    _rbac_env(monkeypatch, admin=True, stack_tables=["t1"])
    user = {"id": "admin1", "credential_surface": "stack"}
    tables = get_accessible_tables(user)
    assert tables is not None, "stack-surface admin must NOT get the god-mode sentinel"
    assert "t1" in tables
    assert can_access_table(user, "t1") is True
    assert can_access_table(user, "t_other") is False


def test_admin_without_key_keeps_god_mode(fresh_db, monkeypatch):
    """Session JWT / scheduler / legacy dict — no key ⇒ unchanged behavior."""
    from src.rbac import get_accessible_tables

    _rbac_env(monkeypatch, admin=True, stack_tables=["t1"])
    assert get_accessible_tables({"id": "admin1"}) is None


def test_non_admin_ignores_surface_value(fresh_db, monkeypatch):
    """surface='all' on a non-admin PAT must not widen anything."""
    from src.rbac import can_access_table, get_accessible_tables

    _rbac_env(monkeypatch, admin=False, stack_tables=["t1"])
    user = {"id": "analyst1", "credential_surface": "all"}
    tables = get_accessible_tables(user)
    assert tables is not None and "t1" in tables
    assert can_access_table(user, "t_other") is False


# ---------------------------------------------------------------------------
# Direct bq.* / full-backtick path pass (app/api/query.py) — the admin
# bypass there skips the per-id grant check, so it must honor the
# credential surface exactly like the src/rbac.py primitives (Devin Review
# on #1090: without the gate a stack-surface admin could read
# out-of-stack tables via a direct bq."<ds>"."<tbl>" reference even
# though the bare-name pass correctly stack-scoped them).
# ---------------------------------------------------------------------------


def _bq_pass_env(monkeypatch, *, admin: bool, row_id: str):
    import app.api.query as query_mod

    monkeypatch.setattr(query_mod, "is_user_admin", lambda uid, conn=None: admin)

    class _FakeRegistryRepo:
        def list_by_source(self, source_type):
            return []  # no registered bare names — pass 1 is a no-op

        def find_by_bq_path(self, bucket, source_table):
            return {
                "id": row_id,
                "name": row_id,
                "bucket": bucket,
                "source_table": source_table,
            }

    monkeypatch.setattr(query_mod, "table_registry_repo", lambda: _FakeRegistryRepo())
    # Unconfigured BQ → the full-backtick pass (pass 3) falls through; the
    # bq."<ds>"."<tbl>" pass (pass 2) is the one under test and both share
    # the same surface-gated `is_admin`.
    monkeypatch.setattr(query_mod, "get_bq_access", lambda: (_ for _ in ()).throw(RuntimeError("unconfigured")))


def _run_bq_pass(user, allowed):
    from app.api.query import _bq_guardrail_inputs

    sql = 'SELECT * FROM bq."ds"."tbl" LIMIT 5'
    return _bq_guardrail_inputs(sql, sql.lower(), None, user, allowed)


def test_direct_bq_path_stack_surface_admin_is_grant_checked(monkeypatch):
    """Out-of-stack direct bq.* reference must 403 for a stack-surface admin."""
    _bq_pass_env(monkeypatch, admin=True, row_id="t_out_of_stack")
    user = {"id": "admin1", "credential_surface": "stack"}
    _, _, blocked = _run_bq_pass(user, allowed=["t_in_stack"])
    assert blocked is not None and blocked["reason"] == "bq_path_access_denied"


def test_direct_bq_path_stack_surface_admin_in_stack_allowed(monkeypatch):
    _bq_pass_env(monkeypatch, admin=True, row_id="t_in_stack")
    user = {"id": "admin1", "credential_surface": "stack"}
    dry_run, _, blocked = _run_bq_pass(user, allowed=["t_in_stack"])
    assert blocked is None
    assert dry_run == [("ds", "tbl", 0)]


def test_direct_bq_path_all_surface_admin_keeps_bypass(monkeypatch):
    """surface='all' (and legacy no-key) admins keep the god-mode bypass."""
    _bq_pass_env(monkeypatch, admin=True, row_id="t_any")
    for user in ({"id": "admin1", "credential_surface": "all"}, {"id": "admin1"}):
        _, _, blocked = _run_bq_pass(user, allowed=None)
        assert blocked is None


# ---------------------------------------------------------------------------
# require_admin_all_surface (app/auth/access.py) -- the POST /api/query/hybrid
# gate (K2, RLS review #1979). This endpoint has no per-table grant check and
# no policy rewrite of its own, so its "is this caller allowed to run raw,
# unfiltered SQL" gate must apply the SAME is_user_admin(...) and
# _credential_surface(user) == 'all' predicate the direct-path guardrails
# above (`_bq_guardrail_inputs` et al. in app/api/query.py, and
# get_accessible_tables/can_access_table in src/rbac.py) already use --
# mirrored here rather than invented fresh, per docs/table-access-
# policies.md's "The admin bypass" section.
# ---------------------------------------------------------------------------


def test_require_admin_all_surface_denies_stack_surface_admin(monkeypatch):
    import app.auth.access as access_mod
    from fastapi import HTTPException

    monkeypatch.setattr(access_mod, "is_user_admin", lambda uid, conn=None: True)

    user = {"id": "admin1", "credential_surface": "stack"}
    with pytest.raises(HTTPException) as exc_info:
        access_mod.require_admin_all_surface(user=user, conn=None)
    assert exc_info.value.status_code == 403
    # Distinct from require_admin's own "Admin access required" -- the
    # caller IS an admin here, just on too narrow a credential surface.
    assert exc_info.value.detail != "Admin access required"


def test_require_admin_all_surface_allows_all_surface_admin(monkeypatch):
    import app.auth.access as access_mod

    monkeypatch.setattr(access_mod, "is_user_admin", lambda uid, conn=None: True)

    user = {"id": "admin1", "credential_surface": "all"}
    assert access_mod.require_admin_all_surface(user=user, conn=None) == user


def test_require_admin_all_surface_allows_admin_without_surface_key(monkeypatch):
    """Legacy dict / session JWT credential -- no key reads as 'all', same
    convention as _credential_surface everywhere else -- unaffected."""
    import app.auth.access as access_mod

    monkeypatch.setattr(access_mod, "is_user_admin", lambda uid, conn=None: True)

    user = {"id": "admin1"}
    assert access_mod.require_admin_all_surface(user=user, conn=None) == user


def test_require_admin_all_surface_denies_non_admin_regardless_of_surface(monkeypatch):
    import app.auth.access as access_mod
    from fastapi import HTTPException

    monkeypatch.setattr(access_mod, "is_user_admin", lambda uid, conn=None: False)

    user = {"id": "analyst1", "credential_surface": "all"}
    with pytest.raises(HTTPException) as exc_info:
        access_mod.require_admin_all_surface(user=user, conn=None)
    assert exc_info.value.status_code == 403


def test_chat_scope_mint_sites_carry_the_claim():
    """Every session-JWT mint site that should be stack-narrowed carries the
    scope claim the resolver keys on. Guards against a silent revert in any
    one site (e.g. broker.py dropping scope='chat' would restore god-mode for
    brokered admin solo sessions without failing any other test)."""
    import inspect

    import app.api.broker as broker_mod
    from app.auth import access as access_mod

    assert '"scope": "chat"' in inspect.getsource(access_mod.mint_session_jwt)
    assert '"scope": "chat"' in inspect.getsource(broker_mod._mint_identity_jwt)
