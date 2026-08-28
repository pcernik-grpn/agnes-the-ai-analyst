"""Git traffic from a chat sandbox to a hosted app's repo.

Before this route the authoring flow had no transport at all, and every layer
said so in a different voice:

- the sandbox reaches Agnes only through the in-sandbox relay (`runner.py`
  rewrites `AGNES_SERVER` to it so "the relay is the only thing that ever holds
  a real credential"), and the relay's `data_apps` ticket is confined by
  `_within_data_apps_prefix` to `/api/data-apps*`;
- a repo lives at `/data-apps.git/<slug>` — a different top-level prefix, so
  the broker refused it;
- going direct was refused by the sandbox's own egress hook, whose allowlist
  cannot name a per-deployment host because the hook ships verbatim.

Watched live on a running instance: the agent fetched a git credential, then
failed to clone by name, by hostname and by IP, including one attempt with the
sandbox bypass. `data_app_git_credential` was handing out a URL that could not
be used from where the agent runs.

This route is the transport, and it differs from its `{method, path, body}`
siblings in two ways that the tests below pin: it PROXIES (git speaks binary
bodies, its own content types, and opens with a GET), and it attaches a
CREDENTIAL rather than an identity JWT, because the git surface authenticates
only a `data-app-git:<slug>` PAT in basic auth.
"""

from __future__ import annotations

import re
from pathlib import Path

BROKER = Path("app/api/broker.py")
RELAY = Path("app/chat/relay.py")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _route_body() -> str:
    src = _read(BROKER)
    start = src.index("async def data_apps_git_broker(")
    return src[start : src.index("\n# Registered as two distinct routes", start)]


# ── the transport exists at all ─────────────────────────────────────────────


def test_the_relay_carries_the_git_prefix_on_the_data_apps_ticket():
    src = _read(RELAY)
    assert '"/data-apps.git": "data_apps"' in src, "the relay must know the prefix and its scope"


def test_git_is_not_an_envelope_route():
    """The `{method, path, body}` envelope is JSON-only. Git's bodies are
    binary pack streams — routing it through the envelope would corrupt them
    (or silently drop them, since the envelope json-decodes and gives up)."""
    src = _read(RELAY)
    envelope = re.search(r"_ENVELOPE_PREFIXES = frozenset\(\{(.*?)\}\)", src, re.DOTALL)
    assert envelope, "_ENVELOPE_PREFIXES moved — re-point this guard"
    assert "data-apps.git" not in envelope.group(1)


def test_the_transparent_leg_passes_the_method_through():
    """Git's first call is `GET /info/refs?service=git-upload-pack`. The
    transparent branch was hardcoded to POST, which turned that into a 405
    before the repo was ever reached."""
    src = _read(RELAY)
    assert "client.request(method.upper(), url, content=body, headers=headers)" in src
    assert "return await client.post(url, content=body, headers=headers)" not in src


def test_the_broker_route_accepts_the_methods_git_uses():
    """Registered per verb, not as one `methods=["GET","POST"]` route: FastAPI
    derives one operation_id per function+path and warns about the duplicate
    otherwise — the same reason `data_apps_git.py` splits its own pair."""
    src = _read(BROKER)
    for method, op in (("GET", "broker_data_apps_git_get"), ("POST", "broker_data_apps_git_post")):
        assert f'methods=["{method}"]' in src and f'operation_id="{op}"' in src


# ── authorization ───────────────────────────────────────────────────────────


def test_the_route_requires_the_data_apps_scope():
    assert '_require_scope(row, "data_apps")' in _route_body()


def test_the_slug_is_pinned_to_an_app_the_ticket_owner_may_reach():
    """Without this a `data_apps` ticket reaches ANY app's repo — and this
    route can push, so it would be a write to someone else's code."""
    body = _route_body()
    assert "data_apps_repo().get_by_slug(slug)" in body
    assert 'app_row.get("owner_user_id") != user["id"]' in body
    assert "is_user_admin" in body
    assert '"forbidden"' in body


def test_a_rejected_slug_is_audited():
    assert "broker_data_apps_git_rejected" in _route_body()


def test_co_sessions_and_scoped_agents_are_refused():
    """A co-session has no single owner to attribute a commit to, and an agent
    session's authority is owner-grants ∩ agent-scope, which says nothing about
    repository access. Both fail closed rather than falling through to the
    owner — the exact mistake `_mint_identity_jwt` documents for its own
    co-session branch."""
    src = _read(BROKER)
    helper = src[src.index("def _ticket_owner_for_git(") : src.index("async def data_apps_git_broker(")]
    assert '"git_not_available_to_co_session"' in helper
    assert '"git_not_available_to_scoped_agent"' in helper
    assert "agent_is_passthrough(agent)" in helper, "a passthrough agent is the ordinary web-chat case"


# ── the credential ──────────────────────────────────────────────────────────


def test_the_credential_is_attached_by_the_broker_not_held_by_the_sandbox():
    """The whole point of the relay is that the sandbox never holds a real
    credential. This route mints one on the server side and puts it in basic
    auth, which is the only thing the git surface authenticates."""
    body = _route_body()
    # `repo_row`, not `app_row`: a draft resolves to its parent's repo
    # before the mint — see the draft test below.
    assert "mint_git_token(repo_row)" in body
    assert 'base64.b64encode(f"agnes:{token}".encode())' in body
    assert '"Authorization": f"Basic {basic}"' in body


def test_the_per_request_credential_is_cleaned_up_even_on_failure():
    """A token minted per request that outlives the request is a row for
    nothing — and a live git credential is not the kind of residue to leave
    lying around. Deleted rather than revoked (Devin Review): revoking left a
    permanent dead row per call in the owner's token list."""
    body = _route_body()
    assert "finally:" in body
    cleanup_at = body.index("access_token_repo().delete(token_id)")
    finally_at = body.index("finally:")
    assert finally_at < cleanup_at, "cleanup must run on the failure path too"


def test_the_query_string_survives():
    """`?service=git-upload-pack` is how smart-HTTP negotiates; dropping it
    turns discovery into a dumb-protocol request against a repo that does not
    serve one."""
    assert "request.url.query" in _route_body()


def test_the_minter_is_reusable_and_returns_the_token_id():
    """`_mint_git_credential` returns a URL, which this route cannot use — it
    needs the raw token plus the id to revoke. Both callers now share one
    minter so the scope and TTL cannot drift apart."""
    src = _read(Path("app/api/data_apps.py"))
    assert "def mint_git_token(row: dict, *, parent_token_id: str | None = None) -> tuple[str, str]:" in src
    cred = src[src.index("def _mint_git_credential(") : src.index("# Cookie carrying a")]
    assert "mint_git_token(row, parent_token_id=parent_token_id)" in cred, (
        "the URL builder must delegate, not duplicate the mint"
    )


def test_the_skill_sends_the_agent_through_the_relay():
    """A transport nobody is told about is not a transport. The skill has to
    name the relay URL AND warn off `data_app_git_credential`'s URL, which is
    the one an agent reaches for and the one that cannot work from a sandbox."""
    skill = Path("app/initial_workspace_default/.claude/skills/agnes-data-apps-extras/SKILL.md")
    body = skill.read_text(encoding="utf-8")
    assert "/data-apps.git/<slug>" in body
    assert "127.0.0.1" in body, "the loopback relay is the reachable origin"
    assert "Do **not** use the URL from `data_app_git_credential(slug)` here" in body


def test_a_draft_is_proxied_to_its_parents_repo():
    """Devin Review on #1252: a draft has no repo of its own.

    `create_draft` never gives one — a draft shares its PARENT's repository.
    Minting the token for the draft slug and aiming the proxy at
    `/data-apps.git/<draft_slug>` therefore fails twice over: the git
    surface refuses a token whose scope names a different repo than the one
    requested, and `repo_path(<draft_slug>)` has no HEAD. That is the path
    the data-apps skill actually walks — it tells the agent to work on a
    draft.
    """
    import inspect

    from app.api import broker

    src = inspect.getsource(broker.data_apps_git_broker)
    assert 'app_row.get("is_draft")' in src, "a draft is still proxied to its own slug"
    assert "mint_git_token(repo_row)" in src, "the token is still scoped to the draft"
    assert 'f"/data-apps.git/{repo_slug}/{path}"' in src, "the target still names the draft"
    # Ownership must still be checked against the app the caller named, not
    # the parent it resolves to.
    assert src.index('app_row.get("owner_user_id")') < src.index('app_row.get("is_draft")')


def test_the_git_protocol_header_is_forwarded():
    """Devin Review on #1252: v2 negotiation never reached the backend.

    Modern git sends `Git-Protocol: version=2` on the initial `info/refs`
    GET, and the git surface turns it into `GIT_PROTOCOL` for `git
    http-backend`. The proxy forwarded exactly two headers, so every brokered
    clone silently fell back to v0 — correct output, but the entire ref
    advertisement on every call instead of the filtered v2 one.
    """
    import inspect

    from app.api import broker

    src = inspect.getsource(broker.data_apps_git_broker)
    assert 'request.headers.get("git-protocol")' in src
    assert '"Git-Protocol"' in src


def test_the_proxy_still_forwards_no_other_client_headers():
    """The allowlist is the point — it must stay an allowlist."""
    import inspect

    from app.api import broker

    src = inspect.getsource(broker.data_apps_git_broker)
    assert "request.headers.items()" not in src, "blanket header forwarding would leak client headers"
    assert "dict(request.headers)" not in src


def test_the_per_request_token_is_deleted_not_just_revoked():
    """Devin Review on #1252: one dead row per git call, forever.

    This token lives for a single brokered call and is minted on every one of
    them — a clone is several — so revoking left a permanent entry in the
    owner's token list, drowning the PATs they actually manage. Revocation is
    for a credential someone might still hold; nobody holds this one but the
    broker, and it is dead before the response returns.
    """
    import inspect

    from app.api import broker

    src = inspect.getsource(broker.data_apps_git_broker)
    assert "access_token_repo().delete(token_id)" in src
    assert "access_token_repo().revoke(token_id)" not in src
    # …and it must still happen in `finally`, or an upstream error leaks it.
    i = src.index("access_token_repo().delete(token_id)")
    assert "finally:" in src[:i]
