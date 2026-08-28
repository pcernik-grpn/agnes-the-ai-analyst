"""The hosted-app service token's `data-app:<slug>` scope must be enforced.

`app/api/data_apps.py::_mint_service_token` mints the credential a running
data app calls the Agnes API with (`AGNES_TOKEN`). Its scope claim was a
label only — `app/auth/pat_resolver.py` gated the two *sibling* data-app
scopes (`data-app-git:`, `data-app-preview:`) fail-closed but read nothing
for this one, so the token was functionally a full-privilege PAT for the
app's owner. Any code running in the container — including an
externally-cloned, less-trusted repo — could call the whole REST API with
it: `/api/admin/*` if the owner is an Admin, and, worse, the
credential-minting routes, where a token that never expires (this one is
minted with `expires_at=None` / `omit_exp=True`) could mint itself further
durable credentials.

The design spec (`docs/superpowers/specs/2026-07-21-data-apps-design.md`
§ "Apps are API clients") documents the surface an app actually needs:
`/api/query` for SQL, `/api/data/...` for parquet, catalog endpoints for
discovery. This pins that surface as a fail-closed allowlist, mirroring
`_AGENT_PAT_ALLOWED_PREFIXES`.
"""

from __future__ import annotations

import hashlib
import types
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


@pytest.fixture
def client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="u1", email="owner@test.com", name="Owner")
    conn.close()

    return TestClient(shared_app)


class _FakeURL:
    def __init__(self, path):
        self.path = path


class _FakeRequest:
    """Same minimal stand-in as tests/test_agent_pat.py."""

    def __init__(self, path, headers=None):
        self.url = _FakeURL(path)
        self.headers = headers or {}
        self.client = None
        self.state = types.SimpleNamespace()


def _mint_scoped_pat(scope: str) -> str:
    """Mint a DB-backed PAT carrying `scope`, exactly as the real minters do.

    The row must exist or the PAT validity chain 401s on `pat_unknown`
    before any scope check runs — the test would then pass for the wrong
    reason.
    """
    from src.repositories import access_token_repo

    token_id = str(uuid.uuid4())
    jwt_token = create_access_token(
        user_id="u1",
        email="owner@test.com",
        token_id=token_id,
        typ="pat",
        omit_exp=True,
        extra_claims={"scope": scope},
    )
    access_token_repo().create(
        id=token_id,
        user_id="u1",
        name=scope,
        token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
        prefix=token_id.replace("-", "")[:8],
        expires_at=None,
    )
    return jwt_token


# --------------------------------------------------------------------------
# Denied: everything outside the documented data surface
# --------------------------------------------------------------------------

# The routes that made this a privilege-escalation rather than merely a
# too-wide credential: each mints a further durable credential, so a
# never-expiring service token could launder itself into a fresh one that
# survives revoking the original.
_CREDENTIAL_MINTING = [
    "/auth/tokens",
    "/api/user/cowork-bundle",
    "/api/mcp-connect/token",
    "/api/cli-auth/rescope-surface",
]

_ADMIN_SURFACE = [
    "/api/admin/tables",
    "/api/admin/metrics",
    "/api/admin/semantic-models",
    "/auth/admin/tokens",
]


@pytest.mark.parametrize("path", _CREDENTIAL_MINTING)
def test_service_token_cannot_reach_a_credential_minting_route(client, path):
    from app.auth.pat_resolver import resolve_token_to_user

    token = _mint_scoped_pat("data-app:demo")
    user, reason = resolve_token_to_user(None, token, _FakeRequest(path))

    assert user is None, f"{path} must not resolve a data-app service token"
    assert reason == "pat_scope_forbidden"


@pytest.mark.parametrize("path", _ADMIN_SURFACE)
def test_service_token_cannot_reach_the_admin_surface(client, path):
    from app.auth.pat_resolver import resolve_token_to_user

    token = _mint_scoped_pat("data-app:demo")
    user, reason = resolve_token_to_user(None, token, _FakeRequest(path))

    assert user is None, f"{path} must not resolve a data-app service token"
    assert reason == "pat_scope_forbidden"


def test_admin_prefix_is_not_shadowed_by_the_allowed_bare_name(client):
    """`/api/metrics` is allowed and `/api/admin/metrics` is not — a naive
    substring test would confuse the two."""
    from app.auth.pat_resolver import resolve_token_to_user

    token = _mint_scoped_pat("data-app:demo")
    user, reason = resolve_token_to_user(None, token, _FakeRequest("/api/admin/metrics"))

    assert user is None
    assert reason == "pat_scope_forbidden"


# --------------------------------------------------------------------------
# Allowed: the surface the spec documents an app needs
# --------------------------------------------------------------------------

_ALLOWED = [
    "/api/query",
    "/api/query/hybrid",
    "/api/data/orders/download",
    "/api/catalog",
    "/api/catalog/tables",
    "/api/metrics",
    "/api/metrics/revenue/mrr",
    "/api/glossary",
    "/api/semantic-models/context",
]


@pytest.mark.parametrize("path", _ALLOWED)
def test_service_token_still_reaches_the_data_surface(client, path):
    """The whole point of the credential — breaking this breaks every hosted
    app in the fleet, and it fails invisibly (container healthy, app renders,
    only its data calls 401)."""
    from app.auth.pat_resolver import resolve_token_to_user

    token = _mint_scoped_pat("data-app:demo")
    user, reason = resolve_token_to_user(None, token, _FakeRequest(path))

    assert reason is None, f"{path} is the documented app data surface"
    assert user is not None and user["id"] == "u1"


# --------------------------------------------------------------------------
# Boundaries
# --------------------------------------------------------------------------


def test_prefix_boundary_is_not_matched(client):
    """`/api/queryevil` must not ride in on the `/api/query` prefix — the
    same boundary the agent-PAT gate pins."""
    from app.auth.pat_resolver import resolve_token_to_user

    token = _mint_scoped_pat("data-app:demo")
    user, reason = resolve_token_to_user(None, token, _FakeRequest("/api/queryevil"))

    assert user is None
    assert reason == "pat_scope_forbidden"


def test_the_git_scope_does_not_fall_through_the_service_gate(client):
    """`data-app-git:` and `data-app:` differ only at the 9th character
    (`-` vs `:`). The git scope must still be refused by its OWN gate, with
    its own reason — not silently reclassified as a service token (which
    would hand the clone credential the whole data surface)."""
    from app.auth.pat_resolver import resolve_token_to_user

    token = _mint_scoped_pat("data-app-git:demo")
    user, reason = resolve_token_to_user(None, token, _FakeRequest("/api/query"))

    assert user is None, "a git clone credential must not reach the data API"
    assert reason == "pat_scope_forbidden"


def test_a_scopeless_pat_is_unaffected(client):
    """An ordinary `agnes init` PAT carries no scope claim and must keep
    reaching every surface it reached before."""
    from app.auth.pat_resolver import resolve_token_to_user

    from src.repositories import access_token_repo

    token_id = str(uuid.uuid4())
    jwt_token = create_access_token(user_id="u1", email="owner@test.com", token_id=token_id, typ="pat")
    access_token_repo().create(
        id=token_id,
        user_id="u1",
        name="plain",
        token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
        prefix=token_id.replace("-", "")[:8],
    )

    user, reason = resolve_token_to_user(None, jwt_token, _FakeRequest("/api/admin/tables"))

    assert reason is None
    assert user is not None and user["id"] == "u1"
