# Microsoft Entra ID OAuth — setup + operator gotchas

The Microsoft provider (`app/auth/providers/microsoft.py`) reads
`MICROSOFT_TENANT_ID`, `MICROSOFT_CLIENT_ID` and `MICROSOFT_CLIENT_SECRET`
straight from environment variables. If any is empty — or the tenant fails the
single-tenant check below — `is_available()` returns `False`, the "Sign in with
Microsoft" button is not rendered, and `/auth/microsoft/*` answers
`microsoft_not_configured`. No other sign-in method is affected.

Sign-in is **authentication only**. The user is created (or matched) through
the shared `ensure_user` provisioning path and lands in the `Everyone` group.
There is no Entra group sync yet — unlike Google, which mirrors Workspace
groups into `user_group_members` at sign-in (see
[`auth-groups.md`](auth-groups.md)). Grant everything else through
[`RBAC.md`](RBAC.md).

This provider is for the **operator's own** tenant, configured at deploy
time. To let users of an **external** organization's Entra tenant sign in —
configured by an admin at runtime, with the external subject (`oid`/`tid`)
captured per user — use the separate `sso` provider instead:
[`auth-sso-entra.md`](auth-sso-entra.md). The two coexist.

## Env vars

| Var | Required for Microsoft | Notes |
|---|---|---|
| `MICROSOFT_TENANT_ID` | yes | The **Directory (tenant) ID** GUID from the app registration's Overview page, or one of the tenant's verified domains (`example.onmicrosoft.com`, `example.com`). The reserved multi-tenant endpoints `common` / `organizations` / `consumers` are **refused** — see below. |
| `MICROSOFT_CLIENT_ID` | yes | The **Application (client) ID** from the same page. |
| `MICROSOFT_CLIENT_SECRET` | yes | A client secret **value** (not its ID) from Certificates & secrets. Entra secrets expire — put the expiry in your calendar; an expired secret surfaces as `/login?error=microsoft_oauth_failed` only. |
| `SESSION_SECRET` | yes | Starlette `SessionMiddleware` stashes the OAuth `state`/`nonce` between `/auth/microsoft/login` and `/auth/microsoft/callback`. Auto-generated to `data/state/.session_secret` if unset; pin it explicitly for multi-replica deployments. |
| `JWT_SECRET_KEY` | yes | Signs the access-token cookie. |
| `FORWARDED_ALLOW_IPS` / `SERVER_URL` / `DOMAIN` | as for Google | Same proxy/redirect-URI concerns — see [`auth-google-oauth.md`](auth-google-oauth.md). |

Enable the provider on the login page with `auth.providers` in
`instance.yaml` (unset = every configured provider except `email`, which is
opt-in only — see `config/instance.yaml.example`):

```yaml
auth:
  providers: [microsoft, password]
  allowed_domain: "example.com"     # read the trust-model note below
```

## Entra app registration

1. Entra admin center → **App registrations** → **New registration**.
2. **Supported account types**: *Accounts in this organizational directory only
   (single tenant)*. Agnes refuses the multi-tenant configuration anyway, but
   matching it here keeps Entra's own consent screen honest.
3. **Redirect URI**: platform *Web*, one per public hostname:
   ```
   https://<your-host>/auth/microsoft/callback
   ```
   Add `http://localhost:8000/auth/microsoft/callback` for local dev.
4. **Certificates & secrets** → **New client secret** → copy the *Value* into
   `MICROSOFT_CLIENT_SECRET`.
5. Copy Directory (tenant) ID + Application (client) ID from **Overview** into
   `MICROSOFT_TENANT_ID` / `MICROSOFT_CLIENT_ID`.

The requested scopes are `openid email profile` — no admin consent needed, no
Microsoft Graph permissions.

## Single tenant is enforced, not assumed

`MICROSOFT_TENANT_ID` is interpolated into the OIDC discovery URL
(`https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration`),
so the three reserved values Microsoft accepts there would silently turn the
provider multi-tenant:

| Value | What it would mean |
|---|---|
| `common` | any work/school **or** personal Microsoft account |
| `organizations` | any work/school account in **any** tenant |
| `consumers` | any personal Microsoft account |

With `auth.allowed_domain` unset, each of those lets any Microsoft account on
earth sign in and self-provision an Agnes account. So Agnes validates the
value: it must be a directory GUID or a verified domain, and the three
reserved names are refused **by name**. A tenant that fails validation leaves
the provider unavailable and logs

```
Microsoft auth check: Microsoft sign-in is DISABLED: MICROSOFT_TENANT_ID='common' names a Microsoft multi-tenant endpoint, not a tenant. …
```

at boot. Silently coming up multi-tenant is the one outcome that is not
allowed; a missing login button plus a boot error is the intended failure mode.

## Trust model — pin `auth.allowed_domain`

One tenant is the **authentication** boundary. It is not by itself the
**identity** boundary, for two reasons:

- **B2B guests.** Accounts invited into the tenant sign in like members, and
  their `email` claim carries their *external* address
  (`someone@othercorp.com`).
- **Agnes matches accounts by address alone.** `ensure_user` looks the address
  up in `users` — there is no provider column and no IdP-subject binding — so a
  successful Microsoft sign-in lands on whatever Agnes account carries that
  address, including one created by Google or password auth.

Therefore: **set `auth.allowed_domain` to the domains you own** whenever
Microsoft sign-in is enabled. When it is unset, boot logs

```
Microsoft auth check: Microsoft sign-in is enabled but auth.allowed_domain is unset. …
```

`auth.allowed_domain` is the **only** control on who may sign in. Two narrower
behaviours exist, but read them for what they are:

- The identity comes from the `email` claim; `preferred_username` (the UPN) is
  used only when `email` is absent — many work/school tenants omit it — and
  only when it is address-shaped. Entra **guest UPNs**
  (`user_othercorp.com#EXT#@tenant.onmicrosoft.com`) are refused there: they
  are not mailboxes, and provisioning an account keyed on one is meaningless.
  **This is not a guard against guests.** Entra emits `email` for guest
  accounts by default, so a guest is resolved from that claim and the UPN
  branch is never reached. Do not read the `#EXT#` refusal as keeping B2B
  guests out — only `auth.allowed_domain` does that, and Entra's default
  `allowInvitesFrom` lets any tenant member invite an outsider.
- `ensure_user` normalizes the address (stripped, lower-cased) before matching,
  so one person cannot end up on two accounts by signing in through two
  providers that disagree on the casing of a claim. `auth.allowed_domain` is
  matched case-insensitively (both sides folded), so the case you write the
  configured domains in does not matter.

## `/login?error=…` codes this provider emits

| Code | Cause | Fix |
|---|---|---|
| `microsoft_not_configured` | One of the three env vars is empty, **or** `MICROSOFT_TENANT_ID` failed the single-tenant check. | `docker compose exec app env \| grep MICROSOFT`, then read the boot log — a validation refusal names the reason. |
| `microsoft_no_email` | The token carried neither an `email` claim nor a usable `preferred_username` (e.g. a B2B guest UPN). | Give the account a mail address in the directory, or sign in with an account that has one. |
| `microsoft_oauth_failed` | Anything raised during the token exchange: expired client secret, redirect-URI mismatch, clock skew, unreachable discovery endpoint. | Check the app log — the exception is logged server-side; the browser only ever sees the code. |
| `domain_not_allowed` | The resolved address's domain is not in `auth.allowed_domain`. | Add the domain (CSV), or sign in with an in-domain account. Shared with the other OAuth providers. |
| `deactivated` | The address maps to a deactivated Agnes account. | Reactivate under `/admin/users`. |

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| No "Sign in with Microsoft" button | Provider unavailable (env var missing / tenant refused), or `microsoft` is not in `auth.providers`. | Boot log first — it distinguishes the two. |
| `AADSTS50011: redirect URI … does not match` | The URI isn't registered, or the app built `http://localhost:8000/...` because `FORWARDED_ALLOW_IPS` isn't set behind the proxy. | Register the URI; set `FORWARDED_ALLOW_IPS=*`; pin `SERVER_URL=https://<your-host>`. |
| Admin page refuses `providers: [microsoft]` with "no usable sign-in method" | The **server** process has no Microsoft env vars (availability is read at process start), or the tenant is invalid. | The 422 detail names the three variables; set them and restart. |
| Login works but `/admin/*` returns 403 | New user is only in `Everyone`. | `SEED_ADMIN_EMAIL` before first login, or `agnes admin break-glass grant-admin <email>` (see [`auth-google-oauth.md`](auth-google-oauth.md)). |
