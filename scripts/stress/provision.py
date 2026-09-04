"""Provision (and tear down) headless identities for a load run.

Creates N service accounts, puts them in one dedicated group, grants that
group the resource the journey needs, and mints one PAT per account. The
identities are the only thing a load run needs that an operator cannot
produce from the runner itself.

Service accounts rather than real users because:

  * ``POST /api/admin/service-accounts`` needs no login provider, so it
    works on an instance whose only doors are SSO and magic link.
  * ``PATCH {"active": false}`` revokes every one of an account's PATs at
    once (``pat_resolver`` checks ``users.active``), so teardown is one
    call per identity and cannot half-succeed.
  * They can never hold an interactive session (``app/auth/jwt.py``
    refuses ``typ="session"`` for ``kind='service'``) and can never join
    Admin (``user_group_members(_pg).add_member``), so a leaked load-test
    PAT is bounded by the grant this script wrote and nothing else.

Minting a PAT is admin AND session-token-only, so the admin token must be an
interactive session JWT (a browser cookie), never another PAT. It is read from
``$AGNES_ADMIN_TOKEN`` or ``--admin-token-file`` and never accepted as a
command-line value — argv is world-readable while the process runs.

The state file holds raw PATs. It is written 0600 and its contents are
never echoed; pass it to the runner by path.

Usage::

    export AGNES_ADMIN_TOKEN="$(cat /path/to/token)"

    python scripts/stress/provision.py create \\
        --base-url https://host --count 20 --state /path/to/identities.json

    python scripts/stress/provision.py teardown \\
        --base-url https://host --state /path/to/identities.json

    python scripts/stress/provision.py verify \\
        --base-url https://host --prefix loadbot
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

DEFAULT_PREFIX = "loadbot"
DEFAULT_GROUP = "loadtest"
DEFAULT_GRANT = "chat:chat"
DEFAULT_TOKEN_TTL_DAYS = 1

# Fallback identities need an address that can never receive mail and can
# never be forced to an external door: `.invalid` is reserved by RFC 2606,
# so no MX exists for it and no operator would list it in
# `auth.allowed_email_domains` (which is what makes an address SSO-forced,
# and an SSO-forced address cannot obtain the session JWT this flow needs).
DEFAULT_EMAIL_DOMAIN = "loadtest.invalid"

# `POST /auth/token` is rate-limited 10/minute. Nine keeps a margin for a
# retry without ever tripping it — the alternative (react to a 429) costs a
# wasted request and a minute of backoff per identity.
_LOGIN_WINDOW_S = 60.0
_LOGIN_PER_WINDOW = 9


@dataclass
class Identity:
    """One provisioned load identity. ``token`` is a raw PAT — never print."""

    idx: int
    slug: str
    account_id: str
    email: str
    token: str


@dataclass
class ProvisionState:
    base_url: str
    group_name: str
    group_id: str
    grant_ids: list[str]
    identities: list[Identity]
    #: Whether THIS run created the group, as opposed to adopting one that
    #: already existed. Teardown purges only what it made: a name collision
    #: with an operator's own group would otherwise delete that group and
    #: every membership and grant on it.
    group_created: bool = False
    #: Which door produced these identities. Teardown differs — a service
    #: account is deactivated through the service-account resource, a user
    #: through the users resource — so the state file must remember which
    #: was used rather than have teardown guess from the email shape.
    identity_kind: str = "service-account"

    def to_json(self) -> dict:
        return {
            "base_url": self.base_url,
            "group_name": self.group_name,
            "group_id": self.group_id,
            "grant_ids": self.grant_ids,
            "group_created": self.group_created,
            "identity_kind": self.identity_kind,
            "identities": [asdict(i) for i in self.identities],
        }

    @staticmethod
    def from_json(raw: dict) -> "ProvisionState":
        return ProvisionState(
            base_url=raw["base_url"],
            group_name=raw["group_name"],
            group_id=raw["group_id"],
            grant_ids=list(raw.get("grant_ids") or []),
            # Absent in a state file written before this field existed. False
            # is the safe default: refuse to delete a group we cannot prove
            # we made.
            group_created=bool(raw.get("group_created", False)),
            identity_kind=str(raw.get("identity_kind") or "service-account"),
            identities=[Identity(**i) for i in raw["identities"]],
        )


@dataclass
class LoginPacer:
    """Keeps `POST /auth/token` calls under the endpoint's own rate limit."""

    stamps: deque = field(default_factory=deque)

    def wait(self) -> None:
        now = time.monotonic()
        while self.stamps and now - self.stamps[0] > _LOGIN_WINDOW_S:
            self.stamps.popleft()
        if len(self.stamps) >= _LOGIN_PER_WINDOW:
            sleep_for = _LOGIN_WINDOW_S - (now - self.stamps[0]) + 0.5
            print(f"[pace] {sleep_for:.0f}s to stay under the login rate limit", file=sys.stderr)
            time.sleep(sleep_for)
            self.stamps.clear()
        self.stamps.append(time.monotonic())


class AdminApi:
    """Thin admin REST client. Raises on unexpected status with the body."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AdminApi":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(self, method: str, path: str, *, json_body: Optional[dict] = None) -> tuple[int, Any]:
        resp = self._client.request(method, path, json=json_body)
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {"raw": resp.text[:500]}
        return resp.status_code, body

    def expect(self, method: str, path: str, *, json_body: Optional[dict] = None, ok: tuple[int, ...]) -> Any:
        status, body = self.request(method, path, json_body=json_body)
        if status not in ok:
            raise SystemExit(f"{method} {path} -> {status}: {json.dumps(body)[:400]}")
        return body


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def preflight(api: AdminApi) -> None:
    """Confirm the caller is an admin holding an INTERACTIVE session.

    Both halves matter and fail differently: a non-admin fails on
    ``/api/admin/groups``, while an admin holding a PAT passes that and
    then fails only at the first mint — after accounts already exist. The
    mint gate (``require_session_token``) has no cheap read-only probe, so
    the check that costs nothing is done here and the credential-kind
    check is left to the first mint, which is why ``create`` mints for
    account #1 before creating account #2.
    """
    status, body = api.request("GET", "/api/admin/groups")
    if status == 401:
        raise SystemExit("admin token rejected (401) — expired session JWT?")
    if status == 403:
        raise SystemExit("token is authenticated but not an admin (403) — ask an admin to run this")
    if status != 200:
        raise SystemExit(f"GET /api/admin/groups -> {status}: {json.dumps(body)[:400]}")
    print(f"[preflight] admin ok, {len(body)} groups visible", file=sys.stderr)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def _find_group(api: AdminApi, name: str) -> Optional[dict]:
    groups = api.expect("GET", "/api/admin/groups", ok=(200,))
    for g in groups:
        if g.get("name") == name:
            return g
    return None


def ensure_group(api: AdminApi, name: str) -> tuple[str, bool]:
    """Return ``(group_id, created_by_this_run)``.

    The second element is what teardown consults before deleting anything:
    reusing a group by name is convenient across a ramp, but it means the
    id in the state file may belong to somebody else's group.
    """
    existing = _find_group(api, name)
    if existing:
        print(f"[group] reusing {name!r} ({existing['id']}) — will NOT be purged", file=sys.stderr)
        return str(existing["id"]), False
    created = api.expect(
        "POST",
        "/api/admin/groups",
        json_body={"name": name, "description": "Ephemeral load-test identities. Safe to delete."},
        ok=(201,),
    )
    print(f"[group] created {name!r} ({created['id']})", file=sys.stderr)
    return str(created["id"]), True


def ensure_grants(api: AdminApi, group_id: str, grants: list[str]) -> list[str]:
    """Grant ``group_id`` each ``resource_type:resource_id`` pair.

    A 409 means the grant is already there — reuse it rather than failing,
    so a re-run after a partial create converges.
    """
    existing = api.expect("GET", "/api/admin/grants", ok=(200,))
    by_key = {(g.get("group_id"), g.get("resource_type"), g.get("resource_id")): str(g.get("id")) for g in existing}
    ids: list[str] = []
    for spec in grants:
        rtype, _, rid = spec.partition(":")
        if not rtype or not rid:
            raise SystemExit(f"--grant must be 'resource_type:resource_id', got {spec!r}")
        key = (group_id, rtype, rid)
        if key in by_key:
            print(f"[grant] reusing {spec}", file=sys.stderr)
            ids.append(by_key[key])
            continue
        created = api.expect(
            "POST",
            "/api/admin/grants",
            json_body={"group_id": group_id, "resource_type": rtype, "resource_id": rid},
            ok=(201,),
        )
        print(f"[grant] created {spec}", file=sys.stderr)
        ids.append(str(created["id"]))
    return ids


def create_identity(
    api: AdminApi,
    *,
    idx: int,
    prefix: str,
    group_id: str,
    token_ttl_days: Optional[int],
    staged: Optional[list] = None,
) -> Identity:
    slug = f"{prefix}-{idx:02d}"
    status, body = api.request(
        "POST",
        "/api/admin/service-accounts",
        json_body={"name": f"Load test {slug}", "slug": slug},
    )
    if status in (404, 405):
        raise SystemExit(
            "this build has no /api/admin/service-accounts route — re-run with "
            "--identity-kind user (creates ordinary password users instead; needs "
            "the 'password' provider enabled, which /login will show)"
        )
    if status == 409:
        # Left over from an interrupted run: adopt it rather than fail.
        accounts = api.expect("GET", "/api/admin/service-accounts", ok=(200,))
        match = next((a for a in accounts if a.get("email", "").startswith(f"{slug}@")), None)
        if match is None:
            raise SystemExit(f"slug {slug!r} is taken but no matching account is listed")
        if not match.get("active", True):
            api.expect("PATCH", f"/api/admin/service-accounts/{match['id']}", json_body={"active": True}, ok=(200,))
        account = match
        print(f"[identity] adopting existing {slug}", file=sys.stderr)
    elif status == 201:
        account = body
    elif status == 501:
        raise SystemExit("service accounts need the Postgres backend (501) — this instance runs DuckDB app-state")
    else:
        raise SystemExit(f"POST /api/admin/service-accounts -> {status}: {json.dumps(body)[:400]}")

    account_id = str(account["id"])
    email = str(account["email"])
    # See the note in create_user_identity: staged before it can hold a PAT.
    if staged is not None:
        staged.append(Identity(idx=idx, slug=slug, account_id=account_id, email=email, token=""))

    member_status, member_body = api.request(
        "POST", f"/api/admin/groups/{group_id}/members", json_body={"email": email}
    )
    if member_status not in (201, 409):
        raise SystemExit(f"add member {email} -> {member_status}: {json.dumps(member_body)[:400]}")

    mint_status, mint_body = api.request(
        "POST",
        f"/api/admin/service-accounts/{account_id}/tokens",
        json_body={
            "name": f"loadtest-{slug}",
            "expires_in_days": token_ttl_days,
            "scope": "loadtest",
        },
    )
    if mint_status == 403:
        raise SystemExit(
            "minting refused (403). --admin-token must be an INTERACTIVE session JWT "
            "(the browser's access_token cookie), not a PAT — see require_session_token."
        )
    if mint_status != 201:
        raise SystemExit(f"mint token for {slug} -> {mint_status}: {json.dumps(mint_body)[:400]}")

    identity = Identity(idx=idx, slug=slug, account_id=account_id, email=email, token=str(mint_body["token"]))
    if staged is not None:
        staged[-1] = identity
    return identity


def create_user_identity(
    api: AdminApi,
    *,
    idx: int,
    prefix: str,
    group_id: str,
    email_domain: str,
    token_ttl_days: Optional[int],
    pacer: LoginPacer,
    staged: Optional[list] = None,
) -> Identity:
    """Fallback for a build with no service-account route: an ordinary user.

    Four calls, because a PAT can only be minted from an interactive
    session and only the account itself can hold one: create the user, set
    a password, exchange it for a session JWT, mint the PAT. The password
    exists only inside this function — it is generated, used twice and
    dropped, never written to the state file, so the durable credential
    left behind is the PAT alone (revoked by deactivating the account).

    ``POST /api/users/{id}/set-password`` sets ``must_change_password``,
    which ``/auth/password/login`` refuses but ``/auth/token`` does not
    check — that asymmetry is what makes this path work without a browser.
    """
    slug = f"{prefix}-{idx:02d}"
    email = f"{slug}@{email_domain}"

    status, body = api.request(
        "POST", "/api/users", json_body={"email": email, "name": f"Load test {slug}", "send_invite": False}
    )
    if status == 201:
        user_id = str(body["id"])
    elif status in (409, 400):
        existing = api.expect("GET", "/api/users", ok=(200,))
        rows = existing if isinstance(existing, list) else existing.get("users", [])
        match = next((u for u in rows if str(u.get("email", "")).lower() == email), None)
        if match is None:
            raise SystemExit(f"{email} rejected ({status}) and not listed: {json.dumps(body)[:300]}")
        user_id = str(match["id"])
        if not match.get("active", True):
            api.expect("POST", f"/api/users/{user_id}/activate", ok=(200, 204))
        print(f"[identity] adopting existing {email}", file=sys.stderr)
    else:
        raise SystemExit(f"POST /api/users -> {status}: {json.dumps(body)[:400]}")

    # Stage the account BEFORE it can hold a credential. Everything after this
    # point can fail — or succeed server-side with its response lost — and the
    # account would still exist. An account missing from the state file is one
    # teardown cannot revoke, which is the only failure here with a lasting
    # consequence. The token is filled in below once it exists.
    if staged is not None:
        staged.append(Identity(idx=idx, slug=slug, account_id=user_id, email=email, token=""))

    password = secrets.token_urlsafe(32)
    pw_status, pw_body = api.request("POST", f"/api/users/{user_id}/set-password", json_body={"password": password})
    if pw_status not in (200, 204):
        raise SystemExit(f"set-password {email} -> {pw_status}: {json.dumps(pw_body)[:300]}")

    member_status, member_body = api.request(
        "POST", f"/api/admin/groups/{group_id}/members", json_body={"email": email}
    )
    if member_status not in (201, 409):
        raise SystemExit(f"add member {email} -> {member_status}: {json.dumps(member_body)[:400]}")

    pacer.wait()
    login_status, login_body = api.request("POST", "/auth/token", json_body={"email": email, "password": password})
    if login_status == 429:
        raise SystemExit(f"login for {email} rate-limited (429) despite pacing — retry the run")
    if login_status != 200:
        raise SystemExit(f"/auth/token {email} -> {login_status}: {json.dumps(login_body)[:300]}")

    session_jwt = str(login_body["access_token"])
    with AdminApi(api.base_url, session_jwt) as as_user:
        mint_status, mint_body = as_user.request(
            "POST",
            "/auth/tokens",
            json_body={"name": f"loadtest-{slug}", "expires_in_days": token_ttl_days, "scope": "loadtest"},
        )
    if mint_status != 201:
        raise SystemExit(f"mint PAT for {email} -> {mint_status}: {json.dumps(mint_body)[:300]}")

    identity = Identity(idx=idx, slug=slug, account_id=user_id, email=email, token=str(mint_body["token"]))
    if staged is not None:
        staged[-1] = identity
    return identity


def cmd_create(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    if state_path.exists():
        # --force may only overwrite a file that holds no credentials.
        # Overwriting one that does is how a run leaves live PATs behind
        # with nothing left on disk that names the accounts holding them —
        # the exact state teardown cannot recover from.
        try:
            stale = ProvisionState.from_json(json.loads(state_path.read_text())).identities
        except Exception:
            stale = []
        if stale:
            raise SystemExit(
                f"{state_path} holds {len(stale)} identities with live tokens — run `teardown` "
                "first. Overwriting it would orphan them."
            )
        if not args.force:
            raise SystemExit(f"{state_path} exists (no identities) — pass --force to overwrite")

    with AdminApi(args.base_url, args.admin_token) as api:
        preflight(api)
        group_id, group_created = ensure_group(api, args.group)
        grant_ids = ensure_grants(api, group_id, args.grant)

        # `identities` is the SAME list the state file is written from, and
        # each create appends its account to it before minting, so the
        # `finally` below persists a half-provisioned account rather than
        # losing it.
        identities: list[Identity] = []
        state = ProvisionState(
            base_url=args.base_url.rstrip("/"),
            group_name=args.group,
            group_id=group_id,
            grant_ids=grant_ids,
            identities=identities,
            group_created=group_created,
            identity_kind=args.identity_kind,
        )
        pacer = LoginPacer()
        try:
            for idx in range(1, args.count + 1):
                if args.identity_kind == "user":
                    ident = create_user_identity(
                        api,
                        idx=idx,
                        prefix=args.prefix,
                        group_id=group_id,
                        email_domain=args.email_domain,
                        token_ttl_days=args.token_ttl_days,
                        pacer=pacer,
                        staged=identities,
                    )
                else:
                    ident = create_identity(
                        api,
                        idx=idx,
                        prefix=args.prefix,
                        group_id=group_id,
                        token_ttl_days=args.token_ttl_days,
                        staged=identities,
                    )
                print(f"[identity] {idx}/{args.count} ready ({ident.email})", file=sys.stderr)
        finally:
            # Always persist what exists so a crash mid-loop is still
            # tearable-down — half-provisioned accounts are the failure
            # mode that leaves credentials behind.
            _write_state(state_path, state)

    print(f"[done] {len(state.identities)} identities -> {state_path} (mode 0600)", file=sys.stderr)
    return 0


def _write_state(path: Path, state: ProvisionState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # The mode argument only applies when open() CREATES the file. Overwriting
    # an existing --force target keeps whatever mode it had, so a 0644 file
    # would receive raw PATs and stay world-readable.
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(state.to_json(), fh, indent=2)


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def cmd_teardown(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    if not state_path.exists():
        raise SystemExit(f"{state_path} not found — nothing to tear down")
    state = ProvisionState.from_json(json.loads(state_path.read_text()))

    failures: list[str] = []
    with AdminApi(args.base_url or state.base_url, args.admin_token) as api:
        for ident in state.identities:
            # Deactivation is what actually revokes the credential — every
            # PAT resolves through the account's `active` flag — so it comes
            # first and a delete failure can never leave a live token behind.
            ok_status: tuple[int, ...]
            if state.identity_kind == "user":
                status, body = api.request("POST", f"/api/users/{ident.account_id}/deactivate")
                ok_status = (200, 204)
            else:
                status, body = api.request(
                    "PATCH", f"/api/admin/service-accounts/{ident.account_id}", json_body={"active": False}
                )
                ok_status = (200,)
            if status == 404:
                print(f"[teardown] {ident.slug} already gone", file=sys.stderr)
                continue
            if status not in ok_status:
                failures.append(f"{ident.slug}: {status} {json.dumps(body)[:200]}")
                continue
            print(f"[teardown] {ident.slug} deactivated", file=sys.stderr)

            if args.purge_identities and state.identity_kind == "user":
                del_status, del_body = api.request("DELETE", f"/api/users/{ident.account_id}")
                if del_status not in (200, 204, 404):
                    failures.append(f"delete {ident.slug}: {del_status} {json.dumps(del_body)[:200]}")
                else:
                    print(f"[teardown] {ident.slug} deleted", file=sys.stderr)

        if args.purge_group and not state.group_created:
            print(
                f"[teardown] group {state.group_name} was adopted, not created by this run — "
                "leaving it and its grants alone",
                file=sys.stderr,
            )
        elif args.purge_group:
            for grant_id in state.grant_ids:
                status, _ = api.request("DELETE", f"/api/admin/grants/{grant_id}")
                if status not in (204, 404):
                    failures.append(f"grant {grant_id}: {status}")
            status, _ = api.request("DELETE", f"/api/admin/groups/{state.group_id}")
            if status not in (204, 404):
                failures.append(f"group {state.group_name}: {status}")
            else:
                print(f"[teardown] group {state.group_name} removed", file=sys.stderr)

        residual = _active_with_prefix(api, args.prefix, state.identity_kind)

    if residual:
        failures.append(f"still active after teardown: {', '.join(residual)}")
    if failures:
        for f in failures:
            print(f"[teardown][FAIL] {f}", file=sys.stderr)
        return 1

    if args.delete_state:
        state_path.unlink()
        print(f"[teardown] {state_path} deleted", file=sys.stderr)
    print("[teardown] clean", file=sys.stderr)
    return 0


def _active_with_prefix(api: AdminApi, prefix: str, identity_kind: str) -> list[str]:
    """Every still-active account whose address starts with ``prefix``.

    Deliberately re-reads the server's own listing rather than trusting the
    state file: the assertion worth making after a teardown is "nothing is
    live", and a state file can only speak for the identities it knows
    about — not for one an interrupted earlier run left behind.
    """
    path = "/api/users" if identity_kind == "user" else "/api/admin/service-accounts"
    listing = api.expect("GET", path, ok=(200,))
    rows = listing if isinstance(listing, list) else listing.get("users", [])
    return [str(r["email"]) for r in rows if str(r.get("email", "")).startswith(prefix) and r.get("active", True)]


def cmd_verify(args: argparse.Namespace) -> int:
    """Post-run assertion: no active identity with our prefix survives."""
    with AdminApi(args.base_url, args.admin_token) as api:
        residual = _active_with_prefix(api, args.prefix, args.identity_kind)
    if residual:
        print(f"[verify][FAIL] {len(residual)} active: {', '.join(residual)}", file=sys.stderr)
        return 1
    print(f"[verify] no active {args.prefix}-* identities ({args.identity_kind})", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _admin_token(token_file: Optional[str]) -> str:
    """Read the admin session JWT from a file or the environment.

    Never from argv. A value on the command line is recorded in shell
    history and readable in `ps` by every process on the machine for as
    long as the call runs, and this credential is an interactive session
    token — the one thing that can mint durable ones.
    """
    if token_file:
        token = Path(token_file).read_text().strip()
        if not token:
            raise SystemExit(f"{token_file} is empty")
        return token
    token = os.environ.get("AGNES_ADMIN_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "no admin token — set AGNES_ADMIN_TOKEN, or pass --admin-token-file <path>. "
            "It is deliberately not accepted as a command-line value."
        )
    return token


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser, *, base_required: bool = True) -> None:
        sp.add_argument("--base-url", required=base_required, help="https://host of the instance")
        # Deliberately NOT a --admin-token value flag: an argument lands in
        # shell history and in every process listing on the host, and the
        # repository's security rules forbid secrets on argv. The token
        # arrives by environment or by file path.
        sp.add_argument(
            "--admin-token-file",
            default=None,
            help="file holding the interactive session JWT; or set $AGNES_ADMIN_TOKEN",
        )
        sp.add_argument("--prefix", default=DEFAULT_PREFIX, help="identity slug prefix")

    c = sub.add_parser("create", help="create N identities + group + grants")
    common(c)
    c.add_argument("--count", type=int, required=True)
    c.add_argument("--group", default=DEFAULT_GROUP)
    c.add_argument(
        "--identity-kind",
        choices=("service-account", "user"),
        default="service-account",
        help="'user' is the fallback for a build with no service-account route: "
        "ordinary password users, which CAN sign in interactively — prefer "
        "service accounts wherever they exist",
    )
    c.add_argument("--email-domain", default=DEFAULT_EMAIL_DOMAIN, help="--identity-kind user only")
    c.add_argument(
        "--grant",
        action="append",
        default=None,
        help=f"resource_type:resource_id (repeatable, default {DEFAULT_GRANT})",
    )
    c.add_argument("--token-ttl-days", type=int, default=DEFAULT_TOKEN_TTL_DAYS)
    c.add_argument("--state", required=True, help="where to write the state file (0600, holds PATs)")
    c.add_argument("--force", action="store_true", help="overwrite an existing state file")
    c.set_defaults(func=cmd_create)

    t = sub.add_parser("teardown", help="deactivate every identity in a state file")
    common(t, base_required=False)
    t.add_argument("--state", required=True)
    t.add_argument("--purge-group", action="store_true", help="also delete the grants and the group")
    t.add_argument(
        "--purge-identities",
        action="store_true",
        help="also DELETE each user (--identity-kind user only); deactivation already revokes the PATs",
    )
    t.add_argument("--delete-state", action="store_true", help="unlink the state file when clean")
    t.set_defaults(func=cmd_teardown)

    v = sub.add_parser("verify", help="assert no active identity with the prefix remains")
    common(v)
    v.add_argument("--identity-kind", choices=("service-account", "user"), default="service-account")
    v.set_defaults(func=cmd_verify)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    args.admin_token = _admin_token(args.admin_token_file)
    if getattr(args, "grant", None) is None and args.cmd == "create":
        args.grant = [DEFAULT_GRANT]
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
