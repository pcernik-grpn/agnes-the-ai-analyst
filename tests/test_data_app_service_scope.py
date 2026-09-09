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
# The admin bypass (§12) must not follow the service token
# --------------------------------------------------------------------------
#
# The scope allowlist above governs WHICH paths the token can reach; it says
# nothing about WHAT the token sees once there. A data app owned by an Admin
# is a second, independent way that Admin-ness could leak through: if the
# token is minted with the repository's default `surface="all"`, every table
# access policy is skipped outright on every read the app makes (see
# `src/access_policy.py::_is_admin_bypass` and
# `docs/table-access-policies.md` -- "The admin bypass"), even though a
# non-admin can be granted the very same app via `_can_view`.


def test_mint_service_token_uses_the_stack_surface(client):
    """`_mint_service_token` must mint with `surface="stack"`, not the
    repository's `surface="all"` default -- otherwise an Admin-owned app
    reads with the admin bypass instead of the owner's filtered view."""
    from app.api.data_apps import _mint_service_token
    from src.repositories import access_token_repo

    owner = {"id": "u1", "email": "owner@test.com"}
    token_id, _jwt = _mint_service_token("demo", owner)

    record = access_token_repo().get_by_id(token_id)
    assert record is not None, "the token row must exist after minting"
    assert record["surface"] == "stack", (
        "a service token minted without surface='stack' lets an admin-owned data app bypass every table access policy"
    )


def test_admin_owned_service_token_does_not_bypass_access_policies(client):
    """Model exactly what `pat_resolver.resolve_token_to_user` stashes on the
    user dict once a service token is minted with `surface="stack"`: an Admin
    owner must still be filtered by `_is_admin_bypass`, the same way a
    `surface='stack'` PAT from `agnes init` is (§12)."""
    from src.access_policy import _is_admin_bypass
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("u1", admin_gid, source="system_seed")
    conn.close()

    principal = {"id": "u1", "email": "owner@test.com", "credential_surface": "stack"}
    assert _is_admin_bypass(principal) is False, (
        "an admin-owned data app's service token must be filtered by access policies, not admin-bypass them"
    )


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


# --------------------------------------------------------------------------
# Viewer data token (`data-app-viewer:<slug>`, typ="data_app_viewer")
# --------------------------------------------------------------------------


class _FakeDataAppsRepo:
    """The resolver reads the app row through the factory; the DuckDB test
    backend has no PG-only `data_identity` column, so stand the row up here
    exactly as a Postgres row would look."""

    def __init__(self, row):
        self.row = row

    def get_by_slug(self, slug):
        return dict(self.row) if self.row and self.row["slug"] == slug else None


def _viewer_app_row(**over):
    row = {
        "id": "app_demo1",
        "slug": "demo",
        "owner_user_id": "u1",
        "repo_mode": "internal",
        "state": "running",
        "service_token_id": "svc-1",
        "data_identity": "viewer",
    }
    row.update(over)
    return row


def _mint_viewer_token(sub="u1", email="owner@test.com", slug="demo", app_id="app_demo1", typ="data_app_viewer", scope=None):
    from datetime import timedelta

    return create_access_token(
        user_id=sub,
        email=email,
        expires_delta=timedelta(seconds=600),
        typ=typ,
        extra_claims={"scope": scope if scope is not None else f"data-app-viewer:{slug}", "slug": slug, "app_id": app_id},
    )


@pytest.fixture
def viewer_env(client, monkeypatch):
    """`client` seeds owner u1; add a second user with no grant on the app and
    route the resolver's repo lookups at the fake row."""
    import src.repositories as repos
    from src.repositories.users import UserRepository
    from src.db import get_system_db

    conn = get_system_db()
    UserRepository(conn).create(id="u2", email="viewer@test.com", name="Viewer")
    conn.close()

    state = {"row": _viewer_app_row()}
    monkeypatch.setattr(repos, "data_apps_repo", lambda: _FakeDataAppsRepo(state["row"]))
    return state


def _resolve_viewer(token, path="/api/query"):
    from app.auth.pat_resolver import resolve_token_to_user

    return resolve_token_to_user(None, token, _FakeRequest(path))


def test_viewer_scope_prefix_does_not_collide_with_the_service_scope():
    """Load-bearing: `"data-app-viewer:x"` must NOT start with `"data-app:"`
    (`-` vs `:` at index 8) or a viewer token would be reclassified as a
    service token and skip its own resolver branch."""
    from app.auth.pat_resolver import DATA_APP_SERVICE_SCOPE_PREFIX, DATA_APP_VIEWER_SCOPE_PREFIX

    assert not (DATA_APP_VIEWER_SCOPE_PREFIX + "x").startswith(DATA_APP_SERVICE_SCOPE_PREFIX)
    assert not (DATA_APP_SERVICE_SCOPE_PREFIX + "x").startswith(DATA_APP_VIEWER_SCOPE_PREFIX)


def test_viewer_token_resolves_to_a_restricted_principal_not_a_user_dict(viewer_env):
    from app.auth.session_principal import PRINCIPAL_TYPES, DataAppViewerPrincipal

    principal, reason = _resolve_viewer(_mint_viewer_token())
    assert reason is None
    assert isinstance(principal, DataAppViewerPrincipal)
    assert isinstance(principal, PRINCIPAL_TYPES)
    assert not isinstance(principal, dict)
    assert principal.slug == "demo"
    assert principal.app_id == "app_demo1"
    assert principal.owner_user_id == "u1" and principal.viewer_user_id == "u1"
    assert principal.viewer_email == "owner@test.com"


def test_viewer_principal_is_denied_admin(viewer_env):
    """`require_admin` hard-denies every restricted principal BEFORE looking
    at the underlying user — even when the viewer (or owner) is an Admin."""
    from fastapi import HTTPException

    from app.auth.access import require_admin

    principal, _ = _resolve_viewer(_mint_viewer_token())
    with pytest.raises(HTTPException) as exc:
        require_admin(user=principal, conn=None)
    assert exc.value.status_code == 403


def test_viewer_token_refused_off_surface(viewer_env):
    """Same fail-closed data surface as the owner's service token."""
    for path in ["/api/admin/users", "/auth/tokens", "/api/data-apps/demo/deploy", "/api/query/hybrid", "/api/sharing/groups"]:
        principal, reason = _resolve_viewer(_mint_viewer_token(), path=path)
        assert principal is None, path
        assert reason == "pat_scope_forbidden", (path, reason)
    # And no request at all (git smart-HTTP, MCP-over-HTTP) -> refused.
    from app.auth.pat_resolver import resolve_token_to_user

    principal, reason = resolve_token_to_user(None, _mint_viewer_token(), None)
    assert principal is None and reason == "pat_scope_forbidden"


def test_viewer_token_refused_when_app_is_back_in_owner_mode(viewer_env):
    """Flipping `data_identity` back kills outstanding tokens on the next
    request — the token bakes in no authority."""
    viewer_env["row"] = _viewer_app_row(data_identity="owner")
    principal, reason = _resolve_viewer(_mint_viewer_token())
    assert principal is None and reason == "pat_scope_forbidden"
    viewer_env["row"] = _viewer_app_row()
    del viewer_env["row"]["data_identity"]  # a DuckDB-shaped row: no column at all
    principal, reason = _resolve_viewer(_mint_viewer_token())
    assert principal is None and reason == "pat_scope_forbidden"


def test_viewer_token_refused_for_a_recreated_or_missing_or_linked_app(viewer_env):
    # slug reused by a NEW row after delete+recreate: app_id no longer matches
    viewer_env["row"] = _viewer_app_row(id="app_other")
    assert _resolve_viewer(_mint_viewer_token()) == (None, "invalid_token")
    # gone
    viewer_env["row"] = None
    assert _resolve_viewer(_mint_viewer_token()) == (None, "invalid_token")
    # linked (externally hosted) apps have no container and never viewer mode
    viewer_env["row"] = _viewer_app_row(repo_mode="linked")
    assert _resolve_viewer(_mint_viewer_token()) == (None, "invalid_token")


def test_viewer_token_refused_when_the_viewer_lost_the_grant(viewer_env):
    """u2 is a real user with no grant on the app (and not its owner) — a
    token minted for them (e.g. before an admin revoked their group's grant)
    is refused live, not at `exp`."""
    principal, reason = _resolve_viewer(_mint_viewer_token(sub="u2", email="viewer@test.com"))
    assert principal is None and reason == "pat_scope_forbidden"


def test_viewer_token_refused_for_a_deactivated_or_unknown_viewer(viewer_env):
    from src.db import get_system_db

    assert _resolve_viewer(_mint_viewer_token(sub="ghost", email="g@test.com")) == (None, "user_not_found")
    conn = get_system_db()
    conn.execute("UPDATE users SET active = false WHERE id = 'u2'")
    conn.close()
    assert _resolve_viewer(_mint_viewer_token(sub="u2", email="viewer@test.com")) == (None, "deactivated")


def test_viewer_typ_with_a_foreign_scope_never_yields_a_user_dict(viewer_env):
    """Either signal alone lands in the viewer branch; the branch then
    requires both. A `typ=data_app_viewer` token wearing some other scope
    must not fall through to the generic path and come back as a user."""
    principal, reason = _resolve_viewer(_mint_viewer_token(scope="cli-login"))
    assert principal is None and reason == "invalid_token"
    principal, reason = _resolve_viewer(_mint_viewer_token(typ="pat"))  # viewer scope, wrong typ
    assert principal is None and reason == "invalid_token"


def test_viewer_principal_binds_row_policies_and_audit_to_the_viewer(viewer_env):
    """The two seams that used to read `owner_user_id` off every principal."""
    from app.api.query import _identity_for_audit
    from src.access_policy import _resolve_identity
    from src.audit_helpers import identity_for_audit

    # Viewer u2 needs a grant to resolve — grant via the owner-equals-viewer
    # shortcut instead: u1 is both. The seams are then checked on a
    # hand-built principal with DISTINCT owner/viewer to prove which side wins.
    from app.auth.session_principal import DataAppViewerPrincipal

    p = DataAppViewerPrincipal(
        slug="demo",
        app_id="app_demo1",
        owner_user_id="u1",
        owner_email="owner@test.com",
        viewer_user_id="u2",
        viewer_email="viewer@test.com",
        intersection={},
    )
    uid, email, _groups = _resolve_identity(p, table_id="t")
    assert (uid, email) == ("u2", "viewer@test.com")
    assert identity_for_audit(p) == ("u2", "viewer@test.com")
    assert _identity_for_audit(p) == ("u2", "viewer@test.com")
