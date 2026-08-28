"""The hosted-app service token's `data-app:<slug>` scope must be enforced.

`app/api/data_apps.py::_mint_service_token` mints the credential a running
data app calls the Agnes API with (`AGNES_TOKEN`). Its scope claim was a
label only — `app/auth/pat_resolver.py` gated the two *sibling* data-app
scopes (`data-app-git:`, `data-app-preview:`) fail-closed but read nothing
for this one, so the token was in practice a full-privilege PAT for the
app's owner. Any code running in the container — including an
externally-cloned, less-trusted repo — could use it against the whole REST
API.

Scope of the escalation, stated precisely (an earlier draft of this fix
overstated it): the PAT-typed service token was ALREADY refused by
`require_session_token` on `/auth/tokens`, `/api/user/cowork-bundle` and
`/api/mcp-connect/token` — that guard rejects any `typ` in `_PAT_LIKE_TYPES`
outright, regardless of scope. What the missing gate really exposed was
`/api/admin/*` whenever the app owner is an Admin, and
`POST /cli/auth/rescope-surface`, which is admin-gated but *requires* a PAT
and mints a fresh 90-day `surface='all'` one. Since the service token is
minted without expiry (`omit_exp=True` / `expires_at=None`), that last one
is a durable-credential laundering path. The denial tests below still cover
the already-closed routes: defence in depth is the point, and a future
refactor of `require_session_token` must not silently re-open them.
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


def _resolve(path: str, scope: str = "data-app:demo"):
    from app.auth.pat_resolver import resolve_token_to_user

    return resolve_token_to_user(None, _mint_scoped_pat(scope), _FakeRequest(path))


# --------------------------------------------------------------------------
# Denied
# --------------------------------------------------------------------------

# Real, currently-mounted paths — verified against the router prefixes, not
# guessed. `/cli/auth/rescope-surface` in particular is NOT under `/api/`
# (`app/api/cli_auth.py` mounts `APIRouter(prefix="/cli/auth")`); an earlier
# draft asserted a `/api/cli-auth/...` path that exists nowhere, which any
# unmatched string would have satisfied.
_CREDENTIAL_MINTING = [
    "/auth/tokens",
    "/api/user/cowork-bundle",
    "/api/mcp-connect/token",
    "/cli/auth/rescope-surface",
    "/cli/auth/exchange",
]

_ADMIN_SURFACE = [
    "/api/admin/tables",
    "/api/admin/metrics",
    "/api/admin/semantic-models",
    "/auth/admin/tokens",
    "/api/v2/metadata-cache/refresh",
]

# Mutating routes that live UNDER an allowed prefix. These are the ones a
# coarse per-router prefix would sweep in for free, which is why the
# allowlist is expressed as exact paths + narrow subtrees rather than one
# entry per router.
_WRITE_ROUTES_UNDER_ALLOWED_PREFIXES = [
    # admin-only BigQuery+local hybrid join — a deliberately non-analyst
    # capability (CLAUDE.md "Hybrid Queries"), reachable under `/api/query`.
    "/api/query/hybrid",
    # create-or-replace of a semantic model by slug when the caller is an
    # Admin — the semantic layer's one write surface, under
    # `/api/semantic-models`.
    "/api/semantic-models/apply",
]


@pytest.mark.parametrize("path", _CREDENTIAL_MINTING)
def test_service_token_cannot_reach_a_credential_minting_route(client, path):
    user, reason = _resolve(path)
    assert user is None, f"{path} must not resolve a data-app service token"
    assert reason == "pat_scope_forbidden"


@pytest.mark.parametrize("path", _ADMIN_SURFACE)
def test_service_token_cannot_reach_the_admin_surface(client, path):
    user, reason = _resolve(path)
    assert user is None, f"{path} must not resolve a data-app service token"
    assert reason == "pat_scope_forbidden"


@pytest.mark.parametrize("path", _WRITE_ROUTES_UNDER_ALLOWED_PREFIXES)
def test_service_token_cannot_reach_a_write_route_under_an_allowed_prefix(client, path):
    user, reason = _resolve(path)
    assert user is None, (
        f"{path} shares a prefix with an allowed read route but is a write/admin "
        "surface — the allowlist must not admit it"
    )
    assert reason == "pat_scope_forbidden"


def test_admin_prefix_is_not_shadowed_by_the_allowed_bare_name(client):
    """`/api/metrics` is allowed and `/api/admin/metrics` is not — a naive
    substring test would confuse the two."""
    user, reason = _resolve("/api/admin/metrics")
    assert user is None
    assert reason == "pat_scope_forbidden"


# --------------------------------------------------------------------------
# Allowed: the surface a hosted app actually uses
# --------------------------------------------------------------------------

# v1 REST — what the design spec documents (`/api/query` for SQL,
# `/api/data/...` for parquet, catalog for discovery) plus the definition
# lookups CLAUDE.md requires before computing a metric. The bundled
# nodejs-dashboard scaffold calls `/api/catalog/tables` and
# `/api/catalog/profile/{name}` directly.
_ALLOWED_V1 = [
    "/api/query",
    "/api/data/orders/download",
    "/api/data/orders/check-access",
    "/api/catalog/tables",
    "/api/catalog/profile/orders",
    "/api/catalog/profile/orders/refresh",
    "/api/catalog/metrics/revenue/mrr",
    "/api/metrics",
    "/api/metrics/revenue/mrr",
    "/api/glossary",
    "/api/glossary/search",
    "/api/glossary/arr",
    "/api/semantic-models/context",
    "/api/semantic-models/schema",
    "/api/semantic-models/search",
    "/api/semantic-models/validate-query",
]

# v2 REST — the surface the `agnes` CLI actually calls. The spec sanctions
# installing the CLI inside an app ("The `agnes` CLI also works if the app's
# `setup.sh` installs it"), and CLAUDE.md's discovery protocol (`agnes
# catalog` / `schema` / `describe` / `snapshot create`) is backed entirely by
# `/api/v2/*`, not the v1 routes. Omitting these would 401 every CLI-using
# app — silently, since the container stays healthy and only its data calls
# fail.
_ALLOWED_V2 = [
    "/api/v2/catalog",
    "/api/v2/schema/orders",
    "/api/v2/sample/orders",
    "/api/v2/scan",
    "/api/v2/scan/estimate",
    "/api/v2/metadata-cache/status",
]


@pytest.mark.parametrize("path", _ALLOWED_V1 + _ALLOWED_V2)
def test_service_token_still_reaches_the_data_surface(client, path):
    """Breaking any of these breaks hosted apps in the fleet, and it fails
    invisibly (container healthy, app renders, only its data calls 401)."""
    user, reason = _resolve(path)
    assert reason is None, f"{path} is part of the documented app data surface"
    assert user is not None and user["id"] == "u1"


# --------------------------------------------------------------------------
# Boundaries
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/queryevil",
        "/api/data-apps",
        "/api/catalogue",
        "/api/metricsx",
        "/api/v2/catalogx",
    ],
)
def test_prefix_boundary_is_not_matched(client, path):
    """A sibling route whose name merely starts with an allowed one must not
    ride in — the same boundary the agent-PAT gate pins."""
    user, reason = _resolve(path)
    assert user is None, f"{path} must not be admitted by prefix confusion"
    assert reason == "pat_scope_forbidden"


def test_the_git_scope_does_not_fall_through_the_service_gate(client):
    """`data-app-git:` and `data-app:` differ only at the 9th character
    (`-` vs `:`). The git scope must still be refused by its OWN gate — not
    silently reclassified as a service token, which would hand the clone
    credential the whole data surface."""
    user, reason = _resolve("/api/query", scope="data-app-git:demo")
    assert user is None, "a git clone credential must not reach the data API"
    assert reason == "pat_scope_forbidden"


def test_the_preview_scope_does_not_fall_through_the_service_gate(client):
    user, reason = _resolve("/api/query", scope="data-app-preview:demo")
    assert user is None
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


def test_no_request_fails_closed(client):
    """Surfaces that pass no `Request` (MCP-over-HTTP, git smart-HTTP) give
    path="" — which matches nothing, so the service token is refused there
    rather than admitted by default."""
    from app.auth.pat_resolver import resolve_token_to_user

    user, reason = resolve_token_to_user(None, _mint_scoped_pat("data-app:demo"), None)
    assert user is None
    assert reason == "pat_scope_forbidden"


# --------------------------------------------------------------------------
# The structural guard
# --------------------------------------------------------------------------


def test_documented_allowlist_names_exist():
    """Every `_DATA_APP_ALLOWED_*` name the module's prose points at must
    resolve.

    Renaming the allowlist left two comments pointing at a
    `_DATA_APP_ALLOWED_PREFIXES` that no longer existed — the reader is sent
    to a symbol they cannot find, which is the same class of defect as a
    guard whose message names the wrong fix.
    """
    import re

    from pathlib import Path

    import app.auth.pat_resolver as mod

    text = Path("app/auth/pat_resolver.py").read_text(encoding="utf-8")
    referenced = set(re.findall(r"[`\"']{1,2}(_DATA_APP_ALLOWED_[A-Z_]+)[`\"']{1,2}", text))
    assert referenced, "guard is looking for the wrong pattern — no names found at all"

    missing = sorted(n for n in referenced if not hasattr(mod, n))
    assert not missing, f"prose in pat_resolver.py names non-existent allowlist symbols: {missing}"


def test_the_admitted_route_set_is_pinned(client, shared_app):
    """Walk the REAL route table and pin every route the allowlist admits.

    The allowlist matches paths, not handlers, so any route added later under
    an allowed path is admitted for free — that is exactly how
    `/api/query/hybrid` (admin-only) and `/api/semantic-models/apply` (a
    write) slipped into an earlier draft of this gate. This test makes that
    silent. When it fails, decide deliberately: either the new route belongs
    on the app surface (add it here) or the allowlist needs narrowing.
    """
    from app.auth.pat_resolver import _data_app_path_allowed

    admitted = set()
    for route in shared_app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        methods = {m for m in (getattr(route, "methods", None) or set()) if m not in ("HEAD", "OPTIONS")}
        if not methods:
            continue
        if _data_app_path_allowed(path):
            admitted.add(path)

    expected = {
        # v1 data + discovery
        "/api/query",
        "/api/data/{table_id}/download",
        "/api/data/{table_id}/check-access",
        "/api/catalog/tables",
        "/api/catalog/profile/{table_name}",
        "/api/catalog/profile/{table_name}/refresh",
        "/api/catalog/metrics/{metric_path:path}",
        "/api/metrics",
        "/api/metrics/{metric_id:path}",
        "/api/glossary",
        "/api/glossary/search",
        "/api/glossary/{glossary_id:path}",
        # semantic layer, read-only members only (never `/apply`)
        "/api/semantic-models/context",
        "/api/semantic-models/schema",
        "/api/semantic-models/search",
        "/api/semantic-models/validate-query",
        # v2 — the surface the `agnes` CLI calls
        "/api/v2/catalog",
        "/api/v2/schema/{table_id}",
        "/api/v2/sample/{table_id}",
        "/api/v2/scan",
        "/api/v2/scan/estimate",
        "/api/v2/metadata-cache/status",
    }

    unexpected = admitted - expected
    assert not unexpected, (
        "these routes are newly reachable by a hosted data app's service token: "
        f"{sorted(unexpected)} — allow them deliberately or narrow the allowlist"
    )
    missing = expected - admitted
    assert not missing, (
        f"these routes are no longer reachable: {sorted(missing)} — if a route moved or "
        "was renamed, hosted apps calling it now fail with a silent 401"
    )
