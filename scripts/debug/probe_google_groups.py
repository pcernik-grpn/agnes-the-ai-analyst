#!/usr/bin/env python3
"""Probe Google Cloud Identity / Admin Directory APIs for "list groups of THIS user".

Run locally with a fresh user OAuth access token to figure out which endpoint
+ scope combo actually works for your Workspace tenant — without a deploy cycle.

Stdlib only — no pip install needed.

Why this exists:
    Zdeněk's first attempt used `cloudidentity.googleapis.com/v1/groups:search`
    with `cloud-identity.groups.readonly` scope. Returns 400 INVALID_ARGUMENT
    in Keboola's Workspace because that endpoint requires admin permission
    despite the scope name suggesting otherwise.

How to get an access token (Easiest path):

    Google's OAuth 2.0 Playground (https://developers.google.com/oauthplayground/)
        1. Click the gear icon (top right) → tick "Use your own OAuth credentials"
        2. Paste your Client ID + Secret (the same OAuth client your Agnes
           deployment uses)
        3. Step 1: pick scopes. For comparison test all of:
              https://www.googleapis.com/auth/cloud-identity.groups.readonly
              https://www.googleapis.com/auth/cloud-identity.groups
              https://www.googleapis.com/auth/admin.directory.group.readonly
              openid
              email
              profile
        4. Authorize APIs → sign in as your Workspace user
        5. Step 2: Exchange authorization code for tokens
        6. Copy the "Access token" string (starts with `ya29.`)

Usage:
    python3 scripts/debug/probe_google_groups.py <access_token> <email>

Example:
    python3 scripts/debug/probe_google_groups.py ya29.a0AfH6S... owner@example.com
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def _section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _probe(name: str, url: str, params: dict | None = None,
           headers: dict | None = None) -> None:
    print(f"\n--- {name} ---")
    full_url = url
    if params:
        full_url = f"{url}?{urllib.parse.urlencode(params)}"
    print(f"  GET {url}")
    if params:
        for k, v in params.items():
            print(f"    {k}={v}")

    req = urllib.request.Request(full_url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            body_bytes = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        body_bytes = e.read()
    except Exception as e:
        print(f"  EXCEPTION: {type(e).__name__}: {e}")
        return

    print(f"  HTTP {status}")
    body = body_bytes.decode("utf-8", errors="replace")
    try:
        body = json.dumps(json.loads(body), indent=2)
    except Exception:
        body = body[:600]
    print("  body:")
    for line in body.splitlines():
        print(f"    {line}")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    access_token, email = sys.argv[1], sys.argv[2]
    auth = {"Authorization": f"Bearer {access_token}"}

    _section("0. Token introspection — what scopes does this token actually have?")
    _probe(
        "tokeninfo",
        "https://oauth2.googleapis.com/tokeninfo",
        params={"access_token": access_token},
    )

    _section("1. OpenID userinfo — verify token identifies the right user")
    _probe(
        "userinfo",
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers=auth,
    )

    _section("2. Cloud Identity — searchTransitiveGroups (user perspective)")
    for label_kind in ("discussion_forum", "security"):
        _probe(
            f"with labels = '{label_kind}'",
            "https://cloudidentity.googleapis.com/v1/groups/-/memberships:searchTransitiveGroups",
            params={
                "query": (
                    f"member_key_id == '{email}' && "
                    f"'cloudidentity.googleapis.com/groups.{label_kind}' in labels"
                ),
            },
            headers=auth,
        )

    _section("3. Cloud Identity — searchDirectGroups (no transitive)")
    _probe(
        "direct only with discussion_forum label",
        "https://cloudidentity.googleapis.com/v1/groups/-/memberships:searchDirectGroups",
        params={
            "query": (
                f"member_key_id == '{email}' && "
                "'cloudidentity.googleapis.com/groups.discussion_forum' in labels"
            ),
        },
        headers=auth,
    )

    _section("4. Cloud Identity — groups:search (admin endpoint, expected to fail)")
    _probe(
        "admin search with parent + member_key_id",
        "https://cloudidentity.googleapis.com/v1/groups:search",
        params={
            "query": (
                "parent == 'customers/my_customer' && "
                f"member_key_id == '{email}' && "
                "'cloudidentity.googleapis.com/groups.discussion_forum' in labels"
            ),
            "view": "BASIC",
        },
        headers=auth,
    )

    _section("5. Admin SDK Directory — legacy groups?userKey (admin scope required)")
    _probe(
        "directory list groups for user",
        "https://admin.googleapis.com/admin/directory/v1/groups",
        params={"userKey": email},
        headers=auth,
    )

    print()
    print("=" * 78)
    print("Interpretation guide:")
    print("=" * 78)
    print("""
  HTTP 200 + groups list  → that's the working endpoint, use it in google.py
  HTTP 200 + empty list   → endpoint works but user has no matching groups
  HTTP 400 INVALID_ARG    → query syntax wrong OR permission issue Google
                            silently disguises as 400 (common for non-admin)
  HTTP 403 PERMISSION     → token lacks scope or admin role
  HTTP 401 UNAUTHENTICATED→ token expired (re-fetch from playground)
  HTTP 404 NOT FOUND      → API not enabled, or wrong URL

  If ALL Cloud Identity endpoints return 400/403 for a non-admin user, the
  conclusion is: Cloud Identity Groups API requires admin permission for
  user-perspective queries, regardless of OAuth scope. Switch to one of:
    (a) Service Account + Domain-Wide Delegation (Vojta's v3 design)
    (b) Workspace OIDC groups claim (admin enables in Workspace Console)
    (c) Grant 'Groups Reader' role to every user (admin overhead)
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
