"""A `data-app-git:` credential dies when the credential that minted it is revoked.

Background — what this is NOT
-----------------------------
It is tempting to read `POST /api/data-apps/{slug}/git-credential` as a
privilege escalation: a PAT calls it and walks away with a git *push*
credential. It is not. The git surface (`app/api/data_apps_git.py`) admits any
token that resolves to a user and computes `allowed = is_owner or admin` for a
push; a plain PAT carries no `scope` claim, so it falls straight through.
`tests/test_data_apps_git.py::test_push_allowed_for_owner` pins exactly that —
an owner's ordinary PAT gets 200 on `git-receive-pack` with no mint call at
all. Gating the mint behind `require_session_token` would therefore break
`agnes app git-credential` (the CLI authenticates with a PAT — see
`cli/config.py::get_token`) while leaving `git push
https://x:<PAT>@host/data-apps.git/<slug>/` untouched. `test_the_mint_is_not_an_
authority_gate` below pins that non-boundary so nobody re-adds the gate.

What IS wrong, and what these tests pin
---------------------------------------
The minted credential is an independent `personal_access_tokens` row with its
own 24-hour life. Revoking the PAT that minted it did nothing to it — so the
one incident-response move that matters ("that PAT leaked, revoke it") left up
to 24 hours of push access on every data app the user owns, silently, on every
surface that can call the mint (CLI, MCP SSE, MCP streamable, the chat
broker). Direct push dies the instant the PAT is revoked; its minted successor
did not. That asymmetry is the real durable-successor property.

The fix binds the successor to its minter via a `parent_token_id` claim
checked in `resolve_token_to_user` — one seam, so every surface that ever
accepts such a token inherits it. An absent claim means "no parent" and behaves
exactly as before (container clone tokens, broker per-request tokens, and every
credential minted before this release).
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from urllib.parse import urlsplit

import pytest


def _basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def _claims(tok: str) -> dict:
    payload = tok.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _token_from_clone_url(url: str) -> str:
    """Pull the credential out of the userinfo half of a clone URL."""
    userinfo = urlsplit(url).netloc.rpartition("@")[0]
    return userinfo.partition(":")[2]


def _seed_main(bare_repo, workdir):
    """One commit on `main` — `POST /{slug}/drafts` branches off it and 409s
    `parent_has_no_main` against an unborn HEAD."""
    import subprocess

    subprocess.run(["git", "clone", str(bare_repo), str(workdir)], check=True, capture_output=True)
    (workdir / "README.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(workdir), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(workdir), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "seed"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(workdir), "push", "origin", "HEAD:main"], check=True, capture_output=True)


@pytest.fixture
def parent_binding_env(e2e_env, shared_app):
    """Owner + admin users with real PAT rows, one data app, its bare repo."""
    import yaml

    from app.auth.jwt import create_access_token
    from src.data_apps.git_repos import init_app_repo
    from src.db import get_system_db
    from src.repositories.access_tokens import AccessTokenRepository
    from src.repositories.data_apps import DataAppsRepository
    from src.repositories.users import UserRepository

    data_dir = e2e_env["data_dir"]
    state = data_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "instance.yaml").write_text(yaml.dump({"data_apps": {"enabled": True}}))
    import app.instance_config as instance_config

    instance_config._instance_config = None

    conn = get_system_db()
    try:
        UserRepository(conn).create(id="owner1", email="owner@test.local", name="Owner")
        DataAppsRepository(conn).create(slug="sales", name="Sales App", owner_user_id="owner1")

        token_repo = AccessTokenRepository(conn)
        pat_id = str(uuid.uuid4())
        pat = create_access_token("owner1", "owner@test.local", token_id=pat_id, typ="pat")
        token_repo.create(
            id=pat_id,
            user_id="owner1",
            name="owner-pat",
            token_hash=hashlib.sha256(pat.encode()).hexdigest(),
            prefix=pat_id.replace("-", "")[:8],
            expires_at=None,
        )
    finally:
        conn.close()

    init_app_repo("sales")
    _seed_main(e2e_env["data_dir"] / "apps" / "git" / "sales.git", data_dir / "seed")

    from fastapi.testclient import TestClient

    return {
        "client": TestClient(shared_app),
        "owner_pat": pat,
        "owner_pat_id": pat_id,
    }


def _revoke(token_id: str) -> None:
    from src.db import get_system_db
    from src.repositories.access_tokens import AccessTokenRepository

    conn = get_system_db()
    try:
        AccessTokenRepository(conn).revoke(token_id)
    finally:
        conn.close()


def _mint(env) -> str:
    r = env["client"].post(
        "/api/data-apps/sales/git-credential",
        headers={"Authorization": f"Bearer {env['owner_pat']}"},
    )
    assert r.status_code == 200, r.text
    return _token_from_clone_url(r.json()["git_clone_url"])


def _push_status(env, credential: str) -> int:
    return (
        env["client"]
        .get(
            "/data-apps.git/sales/info/refs?service=git-receive-pack",
            headers={"Authorization": _basic("x", credential)},
        )
        .status_code
    )


class TestParentBinding:
    def test_the_minted_credential_pushes_while_its_parent_is_live(self, parent_binding_env):
        """The legitimate path — `agnes app git-credential` then `git push` —
        must keep working exactly as before."""
        credential = _mint(parent_binding_env)
        assert _push_status(parent_binding_env, credential) == 200

    def test_the_minted_credential_records_its_parent(self, parent_binding_env):
        credential = _mint(parent_binding_env)
        assert _claims(credential)["parent_token_id"] == parent_binding_env["owner_pat_id"]

    def test_revoking_the_parent_pat_kills_the_minted_credential(self, parent_binding_env):
        """THE regression. Before the fix this stayed 200 for a full 24 hours:
        revoking the leaked PAT did not revoke what it had already minted."""
        credential = _mint(parent_binding_env)
        assert _push_status(parent_binding_env, credential) == 200

        _revoke(parent_binding_env["owner_pat_id"])

        assert _push_status(parent_binding_env, credential) == 401
        # ...and the parent itself is dead too, so there is no re-mint path.
        assert _push_status(parent_binding_env, parent_binding_env["owner_pat"]) == 401

    def test_a_draft_create_credential_is_bound_the_same_way(self, parent_binding_env):
        """`POST /{slug}/drafts` mints the same credential through the same
        helper — it must not be the one surface that forgets the binding."""
        env = parent_binding_env
        r = env["client"].post(
            "/api/data-apps/sales/drafts",
            headers={"Authorization": f"Bearer {env['owner_pat']}"},
            json={"branch": "init"},
        )
        assert r.status_code == 201, r.text
        credential = _token_from_clone_url(r.json()["git_clone_url"])
        assert _push_status(env, credential) == 200

        _revoke(env["owner_pat_id"])
        assert _push_status(env, credential) == 401


class TestBackCompat:
    def test_a_credential_with_no_parent_claim_is_unconstrained(self, parent_binding_env):
        """Absent claim = no parent. Container clone tokens, the broker's
        per-request token, and every credential minted before this release
        carry no `parent_token_id` and must behave exactly as they did."""
        from app.auth.jwt import create_access_token
        from src.db import get_system_db
        from src.repositories.access_tokens import AccessTokenRepository

        tid = str(uuid.uuid4())
        legacy = create_access_token(
            "owner1",
            "owner@test.local",
            token_id=tid,
            typ="pat",
            extra_claims={"scope": "data-app-git:sales"},
        )
        conn = get_system_db()
        try:
            AccessTokenRepository(conn).create(
                id=tid,
                user_id="owner1",
                name="data-app-git:sales",
                token_hash=hashlib.sha256(legacy.encode()).hexdigest(),
                prefix=tid.replace("-", "")[:8],
                expires_at=None,
            )
        finally:
            conn.close()

        assert _push_status(parent_binding_env, legacy) == 200
        # Revoking an unrelated PAT must not touch it.
        _revoke(parent_binding_env["owner_pat_id"])
        assert _push_status(parent_binding_env, legacy) == 200


class TestTheNonBoundary:
    def test_the_mint_is_not_an_authority_gate(self, parent_binding_env):
        """A PAT pushes DIRECTLY, with no mint call. This is why blocking the
        mint (on the MCP transport or behind `require_session_token`) buys
        nothing: the identical credential reaches the same repo one `git push`
        away. Pinned so a future reader does not re-litigate the gate."""
        env = parent_binding_env
        assert _push_status(env, env["owner_pat"]) == 200
