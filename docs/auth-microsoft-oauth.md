# Microsoft Entra ID OAuth — setup + operator gotchas

The Microsoft provider (`app/auth/providers/microsoft.py`) reads
`MICROSOFT_TENANT_ID`, `MICROSOFT_CLIENT_ID` and `MICROSOFT_CLIENT_SECRET`
straight from environment variables. If any is empty — or the tenant fails the
single-tenant check below — `is_available()` returns `False`, the "Sign in with
Microsoft" button is not rendered, and `/auth/microsoft/*` answers
`microsoft_not_configured`. No other sign-in method is affected.

Sign-in always creates (or matches) the user through the shared `ensure_user`
provisioning path, which lands them in the `Everyone` group. Entra ID group
sync — mirroring the signed-in user's group memberships into
`user_group_members`, the way Google mirrors Workspace groups (see
[`auth-groups.md`](auth-groups.md)) — is available but **off by default**;
see [Entra group sync](#entra-group-sync-off-by-default) below. Grant
everything else through [`RBAC.md`](RBAC.md).

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

The requested scopes are `openid email profile` — not admin-restricted, no
Microsoft Graph permissions. That means no admin *has to* approve them, not
that nobody is asked. Absent an organization-wide grant, first sign-in depends
on the tenant's consent policy: under the default one each user clicks through
a consent prompt, and in a tenant that disables user consent the sign-in fails
instead — an Entra policy outcome, not an OAuth misconfiguration, so check it
there before re-reading your client ID and secret. Granting consent once under
Enterprise applications → Permissions → *Grant admin consent* settles both
cases. Turning on Entra group sync (below) widens the scopes to include
`GroupMember.Read.All`, which is admin-restricted and has no user-consent path
at all — see that section for the extra app-registration step.

## Entra group sync (off by default)

Mirrors the signed-in user's Entra ID group memberships into
`user_group_members` (`source='microsoft_sync'`) on every Microsoft
sign-in — the same mechanism [`auth-groups.md`](auth-groups.md) documents
for Google Workspace. Agnes calls `GET https://graph.microsoft.com/v1.0/me/memberOf`
with the delegated access token from the sign-in itself (paged via
`@odata.nextLink`, filtered to `#microsoft.graph.group` entries — a
`directoryRole` or other directory-object membership is ignored), maps each
group's `mail` (or `displayName` when the group isn't mail-enabled) into an
Agnes `user_groups` row via the same get-or-create-by-name mechanism Google
sync uses, and replaces the user's `microsoft_sync`-tagged memberships
wholesale — `admin`/`system_seed` rows and another provider's `google_sync`
rows are untouched.

**Enable it:**

```yaml
auth:
  microsoft:
    group_sync_enabled: true
```

or the env var `AGNES_MICROSOFT_GROUP_SYNC_ENABLED=true` (env wins over
`instance.yaml`). Off by default: enabling it widens the OAuth consent scope
requested at `/auth/microsoft/login` (see below), which reaches every
signed-in user, not only ones who benefit from group sync — so this is an
opt-in decision, not a default a fresh install should inherit silently. The
prefix filter (below) is a separate, env-only knob — set
`AGNES_MICROSOFT_GROUP_PREFIX`, there is no `instance.yaml` key for it.

**Required Entra app permission.** `GET /me/memberOf` needs a delegated
Microsoft Graph permission. Microsoft's own API reference lists `User.Read`
among the least-privileged delegated permissions accepted for this specific
endpoint (reading one's OWN `memberOf`, as opposed to `/users/{id}/memberOf`
for someone else) — but this project deliberately requests the explicit,
narrower-scoped **`GroupMember.Read.All`** (delegated) rather than relying on
whatever `User.Read` happens to already authorize, because that allowance is
tenant-configuration-dependent and not something Agnes can safely assume.
**Flagging the honest uncertainty:** verify the exact permission your tenant
requires against the current Microsoft Graph documentation for
`GET /me/memberOf` before relying on this — Graph's permission tables do
change, and if your tenant refuses `GroupMember.Read.All` for some policy
reason, `Directory.Read.All` (delegated, broader) is the documented
alternative. Either way, this feature needs an explicit **admin consent**
grant — it will not silently start working the moment a user re-consents on
their own:

1. Entra admin center → your app registration → **API permissions** → **Add a
   permission** → **Microsoft Graph** → **Delegated permissions** →
   `GroupMember.Read.All` → **Add permissions**.
2. **Grant admin consent for `<tenant>`** (button on the same page) — this
   step requires a Global Administrator or Privileged Role Administrator;
   an individual user cannot self-consent to this permission in most tenant
   configurations.
3. Set `auth.microsoft.group_sync_enabled: true` (or the env var) and
   **restart** the Agnes process — the wider OAuth scope is requested once,
   at process start (`app.auth.providers.microsoft._setup_oauth`), so
   flipping the switch alone does not widen an already-registered OAuth
   client's request until the next restart. The sync *gate* itself (whether
   `apply_user_groups` calls Graph at all) is read live on every sign-in, so
   between "flip the switch" and "restart", nothing breaks — sign-in
   requests a Graph call with a token that doesn't carry the new scope yet,
   Graph refuses it, and the fetch fails soft (see below) exactly as if the
   feature were still off.

**Fail-soft, never fails login.** Any error along the way — the feature
disabled, an expired/insufficiently-scoped token, a Graph outage, a
malformed response — is logged and treated as "no data": the user's
previous `microsoft_sync` membership snapshot (if any) is left untouched,
and sign-in proceeds. Group sync can never be the reason a user is unable
to log in, with one deliberate exception: the prefix-filter deny gate below.

**Prefix filter.** `AGNES_MICROSOFT_GROUP_PREFIX` (env-only, no
`instance.yaml` key) mirrors Google's `AGNES_GOOGLE_GROUP_PREFIX`: when set,
only fetched groups whose identifier starts with the prefix
(case-insensitive) are mirrored — and if Graph returns at least one group but
NONE match the prefix, the sign-in is refused
(`/login?error=microsoft_not_in_allowed_group`) rather than silently landing
the user with no synced groups. An empty/failed fetch does NOT trigger this
gate (that is the fail-soft rule above) — only a non-empty fetch with zero
prefix matches does, since that is the case where Entra actively told Agnes
"this user has groups, and none of them are yours".

**Local dev / CI**: set `AGNES_MICROSOFT_GRAPH_MOCK_GROUPS` to a
comma-separated list of group identifiers to bypass the real Graph call
entirely (empty value → `[]`; unset → the real HTTP path) — mirrors
`GOOGLE_ADMIN_SDK_MOCK_GROUPS`.

**Not implemented (deliberately out of scope for this feature):** the
admin/everyone system-group email mapping Google sync offers
(`AGNES_GROUP_ADMIN_EMAIL` / `AGNES_GROUP_EVERYONE_EMAIL`) has no Microsoft
equivalent — Entra groups are not required to be mail-enabled, and choosing
an identifier scheme for that mapping (mail vs. object ID vs. display name)
is a separate decision than this feature makes. The admin UI's Google-only
"managed, read-only" group treatment (`app.api.access._is_google_managed`,
`409 google_managed_readonly`) also has no Microsoft equivalent yet — a
`microsoft_sync`-created group is editable/deletable like any other custom
group, which the next sync silently re-creates if deleted (get-or-create by
name) — do not rename or delete a synced group by hand if you want the sync
to keep recognizing it.

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
| `microsoft_not_in_allowed_group` | Entra group sync is enabled with `AGNES_MICROSOFT_GROUP_PREFIX` set, and the user's Graph `memberOf` fetch was non-empty but matched none of it. | Add the user to a matching Entra group, or widen/clear the prefix. Never fires for a failed/empty fetch — see the fail-soft rule in [Entra group sync](#entra-group-sync-off-by-default). |

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| No "Sign in with Microsoft" button | Provider unavailable (env var missing / tenant refused), or `microsoft` is not in `auth.providers`. | Boot log first — it distinguishes the two. |
| `AADSTS50011: redirect URI … does not match` | The URI isn't registered, or the app built `http://localhost:8000/...` because `FORWARDED_ALLOW_IPS` isn't set behind the proxy. | Register the URI; set `FORWARDED_ALLOW_IPS=*`; pin `SERVER_URL=https://<your-host>`. |
| Admin page refuses `providers: [microsoft]` with "no usable sign-in method" | The **server** process has no Microsoft env vars (availability is read at process start), or the tenant is invalid. | The 422 detail names the three variables; set them and restart. |
| Login works but `/admin/*` returns 403 | New user is only in `Everyone`. | `SEED_ADMIN_EMAIL` before first login, or `agnes admin break-glass grant-admin <email>` (see [`auth-google-oauth.md`](auth-google-oauth.md)). |
