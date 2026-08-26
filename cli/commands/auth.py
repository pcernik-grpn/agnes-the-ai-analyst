"""Auth commands — agnes login, agnes logout, agnes whoami, agnes auth import-token."""

import socket

import httpx

from cli.server_moved import is_redirect, moved_server_message
import typer

from cli.client import api_post
from cli.config import (
    save_token,
    clear_token,
    get_token,
    get_server_url,
    resolve_server_url,
    save_config,
    load_config,
)

auth_app = typer.Typer(help="Authentication commands")

# Server-side PAT verification (import-token) hits a live endpoint, so the
# timeout is generous and operator-tunable via AGNES_VERIFY_TIMEOUT (seconds)
# for slow links or a transiently-loaded server.
_VERIFY_TIMEOUT_DEFAULT = 15.0


def _resolve_verify_timeout(raw: str | None) -> float:
    """Verification HTTP timeout in seconds, overridable via AGNES_VERIFY_TIMEOUT.

    Falls back to the default on an unset, non-numeric, or non-positive value so
    a typo can never disable the timeout entirely.
    """
    if not raw:
        return _VERIFY_TIMEOUT_DEFAULT
    try:
        val = float(raw)
    except ValueError:
        return _VERIFY_TIMEOUT_DEFAULT
    return val if val > 0 else _VERIFY_TIMEOUT_DEFAULT


def _warn_verify_unavailable(server_url: str, reason: str) -> None:
    """Warn that server-side PAT verification could not complete, then proceed.

    The token has already been decoded locally and was not rejected (no 401), so
    a transient inability to verify — a read timeout, or a 5xx from an
    overloaded endpoint — must not fail an otherwise-valid credential.
    """
    typer.echo(
        f"Warning: could not verify token against {server_url} ({reason}). "
        "The token decoded locally and was not rejected, so it will be saved. "
        "If a later command reports it invalid, re-run "
        "'agnes auth import-token' once the server is healthy.",
        err=True,
    )


def _manual_token_hint() -> None:
    """Print the fallback path when the browser flow can't be used."""
    server = get_server_url().rstrip("/")
    typer.echo(
        "\nManual fallback — create a token in your browser, then import it:\n"
        f"  1. Open {server}/me/profile#tokens\n"
        "  2. Click 'Create token', name it (e.g. 'cli'), copy it\n"
        "  3. agnes auth import-token --token <paste-token>",
        err=True,
    )


def _require_server(explicit: str | None) -> str:
    """Resolve the server to sign in against, or exit 1 naming the fix.

    Both login paths (browser loopback and ``--password``) run through here
    BEFORE anything else, so neither can fall back to `get_server_url()`'s
    ``http://localhost:8000`` default and send the user to a server that
    isn't there. Exporting ``AGNES_SERVER`` keeps the rest of this process —
    including the manual-fallback hint on failure — on the resolved host;
    writing it to the config is `_persist_server`'s job, once the sign-in has
    actually worked.
    """
    import os

    resolved = resolve_server_url(explicit)
    if not resolved:
        typer.echo(
            "No server configured. Pass --server https://<your-agnes-host>, or set "
            "AGNES_SERVER.\n"
            "  Running an instance on this machine? --server http://localhost:8000",
            err=True,
        )
        raise typer.Exit(1)
    os.environ["AGNES_SERVER"] = resolved
    return resolved


def _persist_server(explicit: str | None, resolved: str) -> None:
    """Save an explicitly-passed ``--server`` — only after a successful login.

    Without this the URL lived in ``AGNES_SERVER`` for one process only, so
    the next command (and `agnes pull`, and `whoami`) resolved back to the
    localhost default until `agnes init` seeded the config. Deliberately
    AFTER the token lands, unlike `agnes auth import-token`: a typo'd host
    that failed to sign in must not become the persisted default.
    """
    if explicit:
        save_config({"server": resolved})


def _login_with_password() -> None:
    """Terminal-only email+password login (no browser).

    The server is already resolved and exported by `_require_server`.
    """
    email = typer.prompt("Email")
    password = typer.prompt("Password", hide_input=True)
    body = {"email": email, "password": password}
    resp = api_post("/auth/token", json=body)
    if resp.status_code == 200:
        data = resp.json()
        save_token(data["access_token"], data["email"])
        typer.echo(f"Logged in as {data['email']}")
        return
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    if resp.status_code == 401 and "external authentication" in str(detail).lower():
        typer.echo(
            "This account has no password — it signs in via Google / magic link. "
            "Run `agnes auth login` (without --password) to use the browser flow.",
            err=True,
        )
    else:
        typer.echo(f"Login failed: {detail}", err=True)
    raise typer.Exit(1)


@auth_app.command()
def login(
    server: str = typer.Option(
        None,
        help="Server URL. Required unless AGNES_SERVER or a saved config already "
        "names one; persisted for later commands when passed.",
    ),
    password: bool = typer.Option(
        False,
        "--password",
        help="Sign in with email + password in the terminal instead of the browser.",
    ),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Print the sign-in URL instead of auto-opening a browser (headless hosts).",
    ),
):
    """Sign in via your browser and store a personal access token.

    Opens your default browser to {server}/cli/auth/start, where you sign in
    with whatever provider your account uses (Google, magic link, or
    password). On approval the server hands a 90-day token straight back to
    the CLI — no copy/paste, no plaintext password in the terminal.

    Use --password for a terminal-only email+password login (rare; only for
    password accounts on a host with no browser), or --no-browser to print the
    URL when no browser can be auto-launched.

    The server comes from --server, then AGNES_SERVER, then the saved config;
    with none of the three this refuses instead of guessing a local instance.
    """
    server_url = _require_server(server)

    if password:
        try:
            _login_with_password()
        except typer.Exit:
            raise
        except Exception as e:
            typer.echo(f"Connection error: {e}", err=True)
            raise typer.Exit(1)
        _persist_server(server, server_url)
        return

    from cli.lib.loopback import capture_code_via_browser

    token_name = f"Agnes CLI ({socket.gethostname()})"[:80]

    if not no_browser:
        # Deliberately NOT echoing a bare `{server_url}/cli/auth/start` here.
        # That form is missing the loopback `port` and `state`, so a user who
        # copied it because no browser appeared would land on a page that
        # cannot hand the code back. `capture_code_via_browser` prints the
        # real, complete URL for every run — that is the one to paste.
        typer.echo("Sign in and approve the request — waiting for it to complete.")

    try:
        code = capture_code_via_browser(server_url, open_browser=not no_browser)
    except TimeoutError as e:
        typer.echo(f"Login timed out: {e}", err=True)
        _manual_token_hint()
        raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"Browser login failed: {e}", err=True)
        _manual_token_hint()
        raise typer.Exit(1)

    try:
        resp = api_post("/cli/auth/exchange", json={"code": code, "name": token_name})
    except Exception as e:
        typer.echo(f"Connection error: {e}", err=True)
        raise typer.Exit(1)

    if resp.status_code == 404:
        typer.echo(
            "This server doesn't support browser login yet (needs a newer Agnes server).",
            err=True,
        )
        _manual_token_hint()
        raise typer.Exit(1)
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        typer.echo(f"Login failed: {detail}", err=True)
        raise typer.Exit(1)

    data = resp.json()
    save_token(data["token"], data["email"])
    _persist_server(server, server_url)
    expires = data.get("expires_at") or "never"
    typer.echo(f"Logged in as {data['email']} (token valid until {expires}).")


@auth_app.command()
def logout():
    """Clear stored token."""
    clear_token()
    typer.echo("Logged out.")


@auth_app.command()
def whoami():
    """Show current user info."""
    token = get_token()
    if not token:
        typer.echo("Not logged in. Run: agnes login")
        raise typer.Exit(1)

    import jwt

    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        typer.echo(f"Email: {payload.get('email', 'unknown')}")
        typer.echo(f"Server: {get_server_url()}")
        from cli.token_status import format_status_line

        typer.echo(f"Token: {format_status_line(token)}")
    except Exception:
        typer.echo("Invalid token. Run: agnes login")
        raise typer.Exit(1)


@auth_app.command("import-token")
def import_token(
    token: str = typer.Option(..., "--token", help="JWT / Personal Access Token to import"),
    server: str = typer.Option(
        None,
        "--server",
        help="Server URL (defaults to ~/.config/agnes/config.yaml or $AGNES_SERVER)",
    ),
    email: str = typer.Option(
        None,
        "--email",
        help="Override email (used only if the JWT lacks an 'email' claim)",
    ),
    skip_verify: bool = typer.Option(
        False,
        "--skip-verify",
        help="Skip the server-side verification step (offline import)",
    ),
):
    """Import a personal access token non-interactively.

    Decodes the JWT locally to extract the email claim, verifies it
    against the server, and writes it to ~/.config/agnes/token.json using the
    canonical format so subsequent `agnes auth whoami` / `agnes pull` calls
    authenticate cleanly.

    Example:

        agnes auth import-token --token "$AGNES_PAT"
        agnes auth import-token --token "$AGNES_PAT" --server https://agnes.example.com
    """
    import os
    import jwt as pyjwt

    # 1) Seed server URL so the verify call below uses the right base URL.
    if server:
        save_config({"server": server})
        os.environ["AGNES_SERVER"] = server
    else:
        cfg = load_config()
        if not os.environ.get("AGNES_SERVER") and not cfg.get("server"):
            typer.echo(
                "No server configured. Pass --server https://<host> or set "
                "AGNES_SERVER, or seed ~/.config/agnes/config.yaml first.",
                err=True,
            )
            raise typer.Exit(1)

    # 2) Decode JWT without signature verification — we only need the claims.
    resolved_email = email
    try:
        payload = pyjwt.decode(token, options={"verify_signature": False})
        resolved_email = resolved_email or payload.get("email")
    except Exception as e:
        typer.echo(f"Could not decode token as JWT: {e}", err=True)
        raise typer.Exit(1)

    # 3) Server-side verification. The server has no dedicated /auth/me — we
    #    use /api/catalog/tables which is the lightest endpoint that every
    #    authenticated user can call and also exercises the PAT validation
    #    path (revocation, expiry, token_hash match).
    #
    #    Only a definitive rejection (401) aborts. A transient inability to
    #    verify — the server unreachably slow (read timeout) or returning a 5xx
    #    from an overloaded endpoint — must NOT fail a token that already decoded
    #    locally: that turned a slow verify into a failed install. Those cases
    #    warn and proceed, exactly as --skip-verify would.
    verify_url = get_server_url()
    if not skip_verify:
        headers = {"Authorization": f"Bearer {token}"}
        resp = None
        try:
            timeout_s = _resolve_verify_timeout(os.environ.get("AGNES_VERIFY_TIMEOUT"))
            with httpx.Client(base_url=verify_url, headers=headers, timeout=timeout_s) as client:
                resp = client.get("/api/catalog/tables")
        except httpx.ConnectError as e:
            # Genuinely unreachable (bad URL / server down) — it can't be used.
            typer.echo(f"Could not reach server {verify_url}: {e}", err=True)
            raise typer.Exit(1)
        except httpx.TransportError as e:
            # Reachable but the call didn't complete (e.g. read timeout).
            _warn_verify_unavailable(verify_url, f"request failed: {e}")
        except Exception as e:
            typer.echo(f"Could not reach server {verify_url}: {e}", err=True)
            raise typer.Exit(1)

        if resp is not None:
            # A moved server answers 3xx here; `httpx.Client` without
            # `follow_redirects` returns it verbatim, and the checks below
            # read it as neither a rejection nor a fault, so login "verified"
            # against a server it never reached. (Devin Review on #1266.)
            if is_redirect(resp.status_code):
                typer.echo(
                    moved_server_message(resp.status_code, resp.headers.get("Location", ""), verify_url), err=True
                )
                raise typer.Exit(1)
            if resp.status_code == 401:
                detail = "unauthorized"
                try:
                    detail = resp.json().get("detail", detail)
                except Exception:
                    pass
                typer.echo(f"Token rejected by server ({verify_url}): {detail}", err=True)
                raise typer.Exit(1)
            if resp.status_code >= 500:
                # Transient server fault (e.g. 503 from an overloaded endpoint) —
                # not a token problem, and the token was not rejected.
                _warn_verify_unavailable(verify_url, f"HTTP {resp.status_code}")
        # 4) Fallback claim lookup via a response the server might include.
        #    /api/catalog/tables doesn't return user info, but other JWT
        #    issuers might later gain an /auth/me. For now, we rely on JWT
        #    claims + the CLI overrides.

    # 5) Refuse to write a partial record if the email claim is missing.
    if not resolved_email:
        typer.echo(
            "Token is missing the 'email' claim. Re-issue the token or pass --email explicitly.",
            err=True,
        )
        raise typer.Exit(1)

    # 6) Persist in the canonical on-disk format used by cli/config.py.
    save_token(token, resolved_email)
    typer.echo(f"Imported token for {resolved_email}.")


@auth_app.command("refresh-groups")
def refresh_groups(
    json_out: bool = typer.Option(False, "--json", help="Print raw JSON."),
):
    """Re-sync your Google Workspace group memberships with the server.

    The Agnes server's snapshot of your Workspace group membership refreshes
    automatically on browser sign-in. If you've been added to a new group
    since your last dashboard login and you're working entirely via the CLI,
    your access (RBAC, marketplace plugin visibility, table grants) won't
    reflect that new group until the snapshot refreshes. This command
    triggers that refresh against the live Admin SDK without requiring a
    browser round-trip.

    Use it when a teammate added you to a group / granted you access and
    you don't see the new plugins / tables yet — instead of signing out and
    back in on the dashboard.
    """
    token = get_token()
    if not token:
        typer.echo("Not logged in. Run: agnes login", err=True)
        raise typer.Exit(1)

    server = get_server_url().rstrip("/")
    try:
        with httpx.Client(
            base_url=server,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        ) as client:
            resp = client.post("/auth/refresh-groups")
    except Exception as e:
        typer.echo(f"Could not reach server {server}: {e}", err=True)
        raise typer.Exit(1)

    if is_redirect(resp.status_code):
        typer.echo(moved_server_message(resp.status_code, resp.headers.get("Location", ""), server), err=True)
        raise typer.Exit(1)
    if resp.status_code != 200:
        detail = resp.text
        try:
            detail = resp.json().get("detail", detail)
        except Exception:
            pass
        typer.echo(
            f"Refresh failed (HTTP {resp.status_code}): {detail}",
            err=True,
        )
        raise typer.Exit(1)

    data = resp.json()

    if json_out:
        import json

        typer.echo(json.dumps(data, indent=2))
        return

    if data.get("denied"):
        typer.echo(
            "Refresh denied: your Workspace groups don't match the "
            "configured group prefix on this Agnes instance — no access "
            "applied. Ask your Agnes admin if you should be a member of an "
            "allow-listed group.",
            err=True,
        )
        raise typer.Exit(2)
    if data.get("soft_failed"):
        typer.echo(
            "Refresh soft-failed: the server could not fetch your "
            "Workspace groups (transient Admin SDK error or empty result). "
            "Your previous group snapshot is unchanged."
        )
        return

    added = data.get("added") or []
    removed = data.get("removed") or []
    current = data.get("current") or []

    if not added and not removed:
        typer.echo(f"Groups already up to date — currently in {len(current)} group(s):")
    else:
        if added:
            typer.echo(f"Added {len(added)} group(s):")
            for g in added:
                typer.echo(f"  + {g}")
        if removed:
            typer.echo(f"Removed {len(removed)} group(s):")
            for g in removed:
                typer.echo(f"  - {g}")
        typer.echo(f"\nNow in {len(current)} group(s):")
    for g in current:
        typer.echo(f"  • {g}")


from cli.commands.tokens import token_app

auth_app.add_typer(token_app, name="token")
