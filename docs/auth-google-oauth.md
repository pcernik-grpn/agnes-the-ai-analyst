# Google OAuth — operator gotchas

The Google OAuth provider (`app/auth/providers/google.py`) reads `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` straight from environment variables. If either is empty, `is_available()` returns `False` and the login page falls back to password auth without complaint (the email magic link is opt-in only — add `email` to `auth.providers` to also offer it).

## Env vars

| Var | Required for Google | Notes |
|---|---|---|
| `GOOGLE_CLIENT_ID` | yes | From Google Cloud Console OAuth 2.0 Client ID (Web application). |
| `GOOGLE_CLIENT_SECRET` | yes | From the same client. Rotate via "Reset secret" on the client; old value is invalidated immediately. |
| `SESSION_SECRET` | yes | Used by Starlette `SessionMiddleware` to stash OAuth `state`/`nonce` between `/auth/google/login` and `/auth/google/callback`. Auto-generated to `data/state/.session_secret` if unset, but for multi-replica or VM-rebuild scenarios pin it explicitly. |
| `JWT_SECRET_KEY` | yes | Signs the access-token cookie. Same auto-generate-and-persist pattern as `SESSION_SECRET`. |
| `FORWARDED_ALLOW_IPS` | only when behind a reverse proxy | Default `127.0.0.1` — uvicorn ignores `X-Forwarded-Proto/Host` from any other client IP, which means callbacks come back as `http://localhost:8000/...` instead of `https://your-host/...`. Set to `*` (or the proxy's IP) when terminating TLS at Caddy / nginx / Cloudflare Tunnel. The compose `command:` already passes `--proxy-headers --forwarded-allow-ips='*'` — this env var is the override. |
| `DOMAIN` | recommended behind TLS | Public hostname (`data.example.com`). Gates the `Secure` flag on the access-token cookie in `google_callback()` — when set, the cookie is only sent over HTTPS, when empty the cookie works over plain HTTP so local dev is unbroken. Also consumed by the Caddy profile. |
| `SERVER_URL` | optional | Absolute base URL (`https://data.example.com`) for externally-visible links — magic-link / password-reset URLs, the MCP issuer, and the `Secure` cookie decision (`app/auth/public_url.py`). It does **not** pin the Google callback: `google_login()` derives `redirect_uri` from `request.url_for("google_callback")`, i.e. from the incoming request as uvicorn resolved it. A callback that comes back on the wrong host is a proxy-header problem (see `FORWARDED_ALLOW_IPS`), not a `SERVER_URL` one. |
| `SEED_ADMIN_EMAIL` | recommended on first boot | App startup (`app/main.py`) creates this user and adds them to the `Admin` system group if missing. Combined with Google OAuth, the first time the matching email signs in, `repo.get_by_email()` finds the seeded record and the user lands as admin. |

## `instance.yaml` requirements that affect auth

`config/loader.py:_validate_config` requires:

- `instance.name`
- `auth.allowed_domain` (CSV — e.g. `"example.com, partner.org"`; empty allows any verified Google account)
- `auth.webapp_secret_key` (typically `"${SESSION_SECRET}"`)
- `server.host`
- `server.hostname`

If any are missing, `app/instance_config.py` catches the `ValueError`, logs `Could not load instance.yaml: ... Using defaults`, and the app keeps running with **empty** instance config. That means `get_allowed_domains()` returns `[]` and **every verified Google account is allowed**. Always grep your runtime log for `Could not load instance.yaml` after a config change — silent fallback is by design (resilience over strictness) but easy to miss.

## OAuth client setup (Google Cloud Console)

1. APIs & Services → Credentials → "Create Credentials" → "OAuth client ID" → "Web application".
2. Authorized redirect URIs — one per public hostname:
   ```
   https://<hostname>/auth/google/callback
   ```
   Add `http://localhost:8000/auth/google/callback` for local dev.
3. The Client ID and Client Secret go into `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.

## Audience: who the client admits

Two gates sit in series, and the Google one runs first. **Audience** (Google
Auth Platform → *Audience*) decides who can reach the consent screen at all;
`auth.allowed_domain` then decides which of those addresses Agnes accepts.
Getting the first one wrong is not a config warning — it is a population that
cannot sign in.

- **Internal** admits only accounts in the Workspace organization that owns
  the Cloud project. Everyone else is refused by Google as `org_internal`. No
  verification, no publishing step, no test-user cap. Internal is scoped to
  the Workspace **account**, so several email domains pass only when they are
  secondary domains of that same account — a group running one tenant per
  company is not one Internal audience.
- **External** admits any Google account, and `auth.allowed_domain` is then
  the entire boundary. Publish it (*Publish app*): in *Testing* the client is
  capped at 100 named test users and each consent expires after 7 days. Since
  the login scopes are `openid email profile` — all non-sensitive — **no
  Google verification is required** to publish.

Which one follows from whose Cloud project holds the client, and an instance
carries exactly **one** Google client (`GOOGLE_CLIENT_ID` is a single value;
`oauth_secret_name_template` gives each VM its own, not each domain):

| Who signs in with Google | Project | Audience |
|---|---|---|
| Only the operator's own staff | operator's | Internal |
| Operator's staff **and** a customer's users on one client | operator's | External, published — the allowlist is the only boundary |
| Only the customer's users | **the customer's** | Internal — see [`auth-google-oauth-customer-setup.md`](auth-google-oauth-customer-setup.md) |

The third row is the tighter arrangement when the instance is hosted for one
customer: Google itself confines the door to their Workspace, and the
operator's own staff come in through `password` (which does not check
`auth.allowed_domain`) or another provider. The trade is that the client, and
therefore the door, is owned by the customer.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `Error 400: redirect_uri_mismatch` | Either the URI isn't registered on the OAuth client, or the app generated `http://localhost:8000/...` because the proxy headers didn't reach uvicorn. | Add the URI in Console; verify `--proxy-headers --forwarded-allow-ips='*'` (already in the bundled compose `command:`) and that the terminator forwards `X-Forwarded-Proto` / `Host`. `SERVER_URL` does not help here — the callback is built from the request. |
| `Error 403: org_internal` | The consent screen's audience is **Internal** and the account is outside the Workspace organization that owns the Cloud project. | See *Audience* above — either the account belongs on the other side of the door, or the client needs to be External (or to live in the other organization's project). |
| `Error 400: admin_policy_enforced` | The signing-in account's Workspace blocks the app under *Security → Access and data control → API controls*. A blanket block covers sign-in scopes too. | Their Workspace admin allows the app, or ticks "Allow access to third-party apps that only require Google sign-in". |
| Login works but the user keeps getting re-prompted on the next request | Access-token cookie lost between requests. Common cause: `DOMAIN` unset → `Secure=False` but the browser hit the app over `https://` via a proxy and dropped the cookie for another reason; or `DOMAIN` set but the browser hit `http://`. | Set `DOMAIN=<hostname>` to match the terminator's hostname, and always serve over HTTPS to the browser. |
| `/login?error=google_not_configured` | `GOOGLE_CLIENT_ID` or `GOOGLE_CLIENT_SECRET` empty in container env. | Inspect `docker compose exec app env \| grep GOOGLE`. |
| `/login?error=domain_not_allowed` | User's email domain isn't in `auth.allowed_domain`. | Add the domain (CSV) and reload — note that allowed_domain only takes effect when `instance.yaml` validates (see above). |
| Login succeeds but `/admin/*` returns 403 | New user is not in the `Admin` system group. | Set `SEED_ADMIN_EMAIL` BEFORE first login, or promote via `agnes admin break-glass grant-admin <email>` (requires shell access to the host — see below). |

## Admin promotion (when `SEED_ADMIN_EMAIL` was missed)

Use the break-glass CLI command. It writes directly to `system.duckdb` without HTTP/auth (the whole point is recovery for the case where the running server's authorization layer cannot help). The DuckDB file must not be locked by a running app process — stop the app first:

```bash
cd <install-dir>
docker compose stop app scheduler
agnes admin break-glass grant-admin me@example.com
docker compose start app scheduler
```

The promoted user must sign out and sign back in — JWTs are issued at login time. Authorization at request time reads from `user_group_members` directly, so the new Admin membership takes effect on the next request without re-issuing the token.
