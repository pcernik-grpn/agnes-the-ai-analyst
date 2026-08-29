# External SSO sign-in (runtime-configured Entra ID OIDC) — setup + trust model

The `sso` provider (`app/auth/providers/sso.py`) is an **optional, additional
login door backed by an external organization's Microsoft Entra ID tenant** —
typically a customer whose users work in an instance the operator hosts.
Unlike the env-var [`microsoft` provider](auth-microsoft-oauth.md) (the
*operator's own* tenant, configured at deploy time), everything here is
configured by an admin **at runtime** — web UI (`/admin/server-config` →
*External SSO sign-in*) or CLI (`agnes admin sso …`) — and read live from the
database: enable, secret rotation and tenant re-pointing take effect on the
next login attempt, no restart.

On every external sign-in Agnes captures the validated token's **`oid`**
(directory object ID — the same ID Microsoft Graph reports) and **`tid`**
(tenant GUID) and stores them bound to the Agnes user, so the rest of the
application can resolve "which external principal is this logged-in user"
(`GET /api/me/external-identity`, the `agnes whoami` *Linked identity* line,
and the server-side repo lookup). Exactly **one** external integration per
instance; the `provider_type` discriminator (`entra_oidc`) leaves room for a
future SAML option without a redesign.

**Requires the Postgres app-state backend.** On a DuckDB-backed instance the
admin API answers a typed `501 requires_postgres_backend` and the provider
simply reads as unavailable.

## Setup — what to exchange with the customer's IdP admin

**You (Agnes admin) → customer IdP admin** ("please register an app for us"):

```
App registration for Agnes single sign-on
=========================================
Platform:      Web
Redirect URI:  https://<agnes-host>/auth/sso/callback
Scopes:        openid email profile   (delegated, Microsoft Graph defaults)
Claims:        no custom claim mapping needed — `oid` and `tid` arrive by
               default; make sure users have a usable email (email claim or
               mail-shaped UPN)
Assignment:    on the app's Enterprise Application, set Properties →
               "Assignment required?" to YES, then assign the intended
               users/groups. Without that setting Entra does NOT block
               unassigned users — any account in the tenant could
               authenticate (Agnes's domain allowlist still applies, but
               the tenant-side gate would be open)
Consent:       grant admin consent once for the organization (Enterprise
               applications → Permissions → "Grant admin consent for
               <tenant>"). Do this even though the scopes above are not
               admin-restricted: without the grant, first sign-in either
               shows every user a consent prompt (default policy) or
               fails outright (tenants that disable user consent), and
               Entra requires administrator consent regardless of policy
               once "Assignment required" is on — which the line above
               asks for. Application Administrator or Cloud Application
               Administrator suffices for these delegated scopes;
               Application Developer does not
```

**Customer IdP admin → you** (entered into the admin panel or `agnes admin sso set`):

- **Directory (tenant) ID** — GUID from the app registration's Overview page
  (a verified domain like `fabrikam.onmicrosoft.com` also works; the reserved
  multi-tenant endpoints `common` / `organizations` / `consumers` are refused,
  same validator as the `microsoft` provider).
- **Application (client) ID.**
- **Client secret value** + its expiry date (calendar the rotation — an
  expired secret surfaces as `/login?error=sso_oauth_failed` only).
- **The email domain(s) to permit** — the customer's own domains only. See
  the trust model below; this list is the whole game. The domain is taken
  from the identity Agnes *resolves* — the `email` claim when the token
  carries one, otherwise a mail-shaped `preferred_username`
  (`resolve_identity`, shared with the `microsoft` provider) — and that
  address need not sit on the organization's public domain: a tenant
  without a verified vanity domain typically asserts
  `<user>@<tenant>.onmicrosoft.com`. Deriving the list from the customer's
  website, or from the address you exchange mail with, fails closed as
  `domain_not_allowed`. The test sign-in in step 4 below prints the
  resolved identity, so run it before you settle the list.

## Configure, prove, enable

1. **Save the config** (disabled): tenant ID, client ID, button label
   (rendered as *Sign in with {label}*), allowed email domains.
   ```bash
   agnes admin sso set --tenant-id <guid> --client-id <guid> \
       --display-name "Fabrikam" --domains "fabrikam.com"
   ```
2. **Store the client secret** (hidden prompt; encrypted at rest under
   `AGNES_VAULT_KEY`, never echoed back by any endpoint):
   ```bash
   agnes admin sso set-secret
   ```
3. **Probe the config server-side** — validates the tenant, checks the secret
   decrypts, and fetches the tenant's OIDC discovery document:
   ```bash
   agnes admin sso test
   ```
4. **Run the admin test sign-in** (web UI: *Test sign-in* on the SSO panel,
   i.e. `GET /auth/sso/login?mode=test`). This is an end-to-end proof against
   a tenant you cannot see into (redirect URI registered? secret valid?
   claims present?): admin-only, works **before** enabling and independent of
   `auth.providers`, completes the real code exchange, and renders the
   resolved email, `oid`, `tid`, the domain-allowlist verdict and which Agnes
   account the login *would* attach to — **without** creating a user, writing
   an identity, or issuing a session.
5. **Enable** — the button appears on the login page instantly, and is just
   as instantly reversible:
   ```bash
   agnes admin sso set --enable
   ```

`agnes admin sso status` shows the live state (config, secret presence, vault
key) at any point. An enabled config is announced in the boot log, naming the
trusted tenant and the permitted domains.

## Trust model — the domain allowlist is the boundary

**The external tenant's admin can assert any email.** Entra (like any IdP)
puts whatever email the tenant admin configures into the token. On a user's
first-ever SSO login Agnes attaches by email (the shared `ensure_user` path:
match case-insensitively, or JIT-create with `Everyone` membership only) — so
whoever controls the customer tenant can, for any email in the *permitted
domains*, attach to (or create) the matching Agnes account. Two layers keep
that contained:

- **Before an account links:** `allowed_email_domains` is **required and
  explicit** — it does *not* inherit the instance-level
  `auth.allowed_domain`, precisely so the customer tenant's assertion power
  never silently extends over operator accounts. List only domains the
  external tenant is entitled to assert (the customer's own domains, never
  yours). Empty list ⇒ the provider cannot be enabled, and the callback
  fails closed regardless. The check runs before any user is created.
- **After an account links:** the identity is pinned to the validated token's
  `(tid, oid)` pair. Subject binding beats email — an email change in the
  customer tenant cannot re-target the login onto a different existing Agnes
  account (drift is logged, never rewritten). A *different* principal
  resolving to the same mailbox (canonically: email recycling after
  offboarding) is **refused** with `sso_identity_conflict`; the recovery is
  an explicit admin decision (`agnes admin sso unlink <user-id>`, or
  deactivate the old account).

**B2B guests** invited into the customer tenant authenticate too, and their
`email` claim carries their external address — tenant pinning does not stop
them; the domain allowlist does. The authorize redirect forces
`prompt=select_account` so a user holding several Entra sessions (or a guest
session) gets the account picker instead of a silent SSO into a
domain-allowlist refusal.

**Allowlisted domains are forced off the local credential doors.** While
the config is enabled (and `sso` is offered under `auth.providers`), the
password and magic-link doors — login, `/auth/token`, forgot-password,
invite and magic-link legs alike, including redemption of links minted
before the domain joined the allowlist — refuse every address whose domain
is in `allowed_email_domains`. This is what makes the delegation real: when
the customer tenant offboards someone, no previously set password or
bookmarked link keeps their Agnes access alive. Browser forms redirect such
addresses to `/auth/sso/login`; JSON credential endpoints answer their
usual generic refusal (no domain oracle). Existing password hashes are left
in place, just unusable — remove the domain from the allowlist (or disable
SSO) and those doors open again; nothing is destroyed. The scope is the
**local credential doors only**: the OAuth providers (google, microsoft,
keboola) are separate doors with their own domain policies, unchanged by
the forcing — if one of them is enabled and its policy admits an
allowlisted domain, it remains a way in, so keep those policies from
overlapping the SSO allowlist if the offboarding guarantee is to be
complete. Within its scope the external IdP's MFA/conditional-access
posture protects the only door these domains have.

**Accepted risks (v1), documented rather than mitigated:** first-login email
attach itself (same semantics as every Agnes provider); no session revocation
on unlink/disable (sessions are 30-day stateless JWTs — `users.active` is the
kill switch, as everywhere); and the operator trusts the customer tenant's
hygiene *within the permitted domains* (offboarding, guest curation). The
audit trail (`sso.identity.linked` rows, `email_at_link`, `last_login_at`)
is the detective control.

## Captured identities

- `GET /api/me/external-identity` — the caller's own linkage
  (`{linked, provider_type, tenant_id, subject, linked_at, last_login_at}`);
  also the *Linked identity* line on `/me/profile` and in `agnes whoami`.
- `agnes admin sso identities [--limit --offset --json]` — all linked
  identities, newest first, paginated.
- `agnes admin sso unlink <user-id>` — hard-deletes one binding (the
  mis-attach recovery tool). The user row is untouched; their next SSO
  sign-in re-attaches by email.
- Deliberately **no JWT claim** carries the external identity: a claim would
  snapshot link state into a 30-day token with no kill switch. Server-side
  consumers resolve it live via
  `user_external_identities_repo().get_by_user_id(user_id)`.

## Lifecycle & lockout protection

- **Disable / clear-secret / delete-config are last-login-door guarded**:
  each answers `422 last_login_door` when the operation would leave the
  instance with **no usable sign-in method** (e.g. `auth.providers: [sso]`
  with no user holding a password). Open another door first — set a password
  for an admin, or widen `auth.providers`. Break-glass with server access:
  the `AGNES_AUTH_PROVIDERS` env override (env wins over instance.yaml) plus
  the registry's password/email rescue.
- **Domain forcing follows the live config** — it applies exactly while the
  provider is enabled, complete and offered, with no persistent state of its
  own. Each of the guarded operations above also lifts the forcing, which is
  why a password holder inside a forced domain still satisfies the guard:
  their password door is the one that reopens the moment the operation
  lands.
- **Deleting the config keeps the identity rows** — they are historically
  true links, inert unless the same tenant is configured again, purgeable
  per-user via `unlink`.
- **Re-pointing at a different tenant** needs no cleanup: stale bindings are
  unreachable by the login lookup and self-heal (are replaced, loudly
  logged) on each user's next successful sign-in through the active config.
- A secret that no longer decrypts (rotated/malformed `AGNES_VAULT_KEY`)
  reads as *unset*: the provider turns unavailable and the boot log carries a
  loud error — it never 500s.

## `/login?error=…` codes this provider emits

| Code | Meaning |
|---|---|
| `sso_not_configured` | Provider not configured/complete (or the secret no longer decrypts). |
| `sso_oauth_failed` | The OAuth exchange failed (expired secret, misregistered redirect URI, Entra-side error). Details are in the server log, `repr()`-escaped. |
| `sso_no_email` | The token carried neither an `email` claim nor a mail-shaped UPN (B2B guest `#EXT#` UPNs are refused as identities). |
| `sso_no_subject` | The token carried no `oid` claim (the `sub` claim is deliberately never used — it is pairwise per app registration). |
| `sso_wrong_tenant` | The token's `tid` does not match the configured tenant GUID (belt-and-braces on top of the tenant-pinned issuer validation). |
| `domain_not_allowed` | The resolved email's domain is not in `allowed_email_domains` (shared copy with the other providers). |
| `sso_identity_conflict` | A different external principal already resolves to this mailbox's account, or vice versa — an admin must unlink or deactivate. |
| `deactivated` | The bound Agnes account is deactivated. |

## Common failure modes

- **Button missing from the login page** → `agnes admin sso status`: the
  provider renders only when configured + secret decryptable + enabled +
  (`auth.providers` unset or naming `sso`) + Postgres backend.
- **Every sign-in dies on `domain_not_allowed`** → the user picked (or Entra
  silently reused) an account outside the permitted domains — the account
  picker is forced, so re-try and pick the right one; or the allowlist is
  missing the customer domain.
- **`sso_oauth_failed` right after it worked yesterday** → the customer's
  client secret expired; ask for a new value and `agnes admin sso set-secret`.
- **Unassigned customer users can start a sign-in** → the customer forgot
  *Assignment required = Yes* on their Enterprise Application (see the
  checklist); Agnes's allowlist still refuses foreign domains, but the
  tenant-side gate should be closed too.
- **A permitted-domain user reports password login / forgot-password
  bouncing them to the SSO sign-in** → working as designed: their domain is
  in `allowed_email_domains`, so the SSO door is the only one that opens
  (see the trust model). If that user genuinely should not be federated,
  their domain does not belong on the allowlist.
