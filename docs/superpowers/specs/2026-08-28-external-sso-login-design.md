# External identity login (runtime-configured Entra ID OIDC)

**Date:** 2026-08-28
**Status:** Designed, pre-implementation

## Motivation

An Agnes instance is operated by one organization but its users may belong to
another: a customer whose identities live in the customer's own IdP. Today the
instance operator picks login providers at deploy time (`auth.providers`,
env-var credentials), and the one Entra ID provider Agnes has
(`app/auth/providers/microsoft.py`) is configured through env vars read at
import time — repointing it means editing the deployment and restarting.

This design adds **one additional, customer-owned external identity login per
instance**, configured by an admin at runtime in the web UI:

1. The admin enters the customer tenant's details (tenant ID, client ID,
   client secret, permitted email domains, a button label) on the admin page —
   no instance.yaml edit, no env vars, no restart.
2. The login page then offers the instance's current provider(s) **plus** the
   external-identity button. Both keep working; neither disables the other.
3. On every external sign-in Agnes captures the asserted external subject —
   Entra's `oid` (directory object ID) and `tid` (tenant ID) — and stores it
   bound to the Agnes user, so the rest of the application can resolve "which
   external principal is this logged-in user" (the eventual consumer is
   row-level filtering of data ingested from systems that key permissions on
   that same object ID, e.g. SharePoint via Microsoft Graph — the filtering
   itself is out of scope here).

Worked example used throughout: operator `example.com` hosts an instance used
by customer **`fabrikam.com`**, whose users sign in with their Fabrikam Entra
ID accounts.

Exactly **one** external integration per instance. Multi-IdP is a
non-goal; the config model is a singleton by construction.

## Why OIDC now, SAML later

The requirement is "SAML or Entra ID, whichever is simpler". The answer is
lopsided:

- **Agnes already has a complete, hardened Entra OIDC provider.** The
  `microsoft` module ships single-tenant enforcement (`tenant_id_error()`
  refuses `common`/`organizations`/`consumers` and the well-known consumer
  GUIDs), identity resolution with B2B-guest-UPN refusal
  (`resolve_identity()`), attacker-influenced-error-log discipline, and the
  full authlib callback flow. The delta for this feature is configuration
  source and identity capture, not protocol work.
- **SAML would be greenfield plus native dependencies.** The repo has zero
  SAML code. The maintained Python SP libraries (python3-saml, pysaml2) both
  require the `xmlsec`/`libxmlsec1` native stack, with well-documented Docker
  build and libxml2-version-mismatch pain, and bring the XML-signature attack
  surface (XSW et al.) that SAML SPs must actively defend against. A sibling
  product's SAML rollout QA log records fifteen implementation bugs and a
  string of Entra-specific SAML quirks (empty `user.mail` on cloud-only
  accounts requiring a claim-source change, group claim source settable only
  in the portal UI, `requestedAuthnContext` breaking stronger auth methods
  with AADSTS75011). None of that class of problem exists on the OIDC path.
- **OIDC delivers the required identifier natively.** With the scopes the
  `microsoft` provider already requests (`openid email profile`), Entra's ID
  token carries `oid` — the immutable per-tenant object ID that Microsoft
  Graph returns as the user's `id`. That is exactly the identifier the
  downstream filtering needs. (SAML has an equivalent claim, but OIDC gets it
  with zero extra configuration on the customer side.)

So: **v1 implements OIDC against Entra ID only.** The config row carries a
`provider_type` discriminator (`'entra_oidc'`) so a future SAML integration
can slot into the same singleton config, admin surface, and identity table
without a redesign — but no SAML code, schema, or dependency ships now.

## Scope — four pieces

### 1. A new `sso` provider slot

New module `app/auth/providers/sso.py`, a sixth entry in the existing
provider convention (`router` + `is_available()` + `startup_warnings()`,
registered unconditionally in `app/main.py`). Routes `/auth/sso/login` and
`/auth/sso/callback`; error codes `sso_*`.

One deliberate deviation from the other five providers: the router does
**not** carry the router-level `require_provider("sso")` dependency.
Instead each route gates inline — **normal mode** enforces
`provider_allowed("sso") and is_available()` and answers the same 404 a
disallowed provider answers today (posture unchanged for users); **test
mode** (`?mode=test`, and the callback leg carrying the test marker)
requires an *admin session* plus `is_configured()` and deliberately ignores
the allowlist and the `enabled` flag. A router-level dependency would make
the pre-enable admin test unreachable exactly when it is needed: with an
explicit `auth.providers` list not yet naming `sso`, or with `[sso]` alone
while the provider is still disabled (the registry's allowlist rescue then
swaps in `password`/`email` and `require_provider("sso")` 404s — including
the Entra redirect back to `/auth/sso/callback`).

**Why a new slot and not making `microsoft` DB-configurable** (considered and
rejected):

1. *Coexistence.* An instance whose operator login is already env-configured
   Entra (the operator's own tenant) must be able to also carry the
   customer-tenant login. Authlib registers one client per provider name and
   the discovery URL is per-tenant — one `microsoft` name cannot serve two
   tenants, and env-vs-DB precedence would be exactly the ambiguous state the
   admin API documents away today ("Microsoft sign-in cannot be configured
   from instance.yaml", `app/api/admin.py`).
2. *Runtime reconfig would rewrite `microsoft.py` anyway.* Its config is
   captured at module import (`MICROSOFT_CLIENT_ID = os.environ.get(...)`,
   `_setup_oauth()` once). Converting a working, hardened provider to live
   reads is most of the cost of a new module, with regression risk on top.
3. *Branding.* The external button needs a configurable label ("Sign in with
   Fabrikam"), its own error copy, and its own operator docs.

What the new module does **not** duplicate: the pure functions
`tenant_id_error()`, `_is_directory_guid()`, `_is_verified_domain()`,
`resolve_identity()`, `_upn_is_usable_identity()` are imported from
`app.auth.providers.microsoft` directly (importing that module is
side-effect-free beyond its idempotent `_setup_oauth()` no-op, and
`app/main.py` imports it unconditionally already). No extraction into a
shared module in v1 — that would churn a hardened file for zero behavior
change; extract only if an import cycle ever forces it.

The slot name is deliberately generic (`sso`, not `entra`): the protocol
lives in `provider_type` inside the config row. One instance = one external
IdP = one slot.

### 2. Runtime configuration: `sso_config` + vault-encrypted secret

A new **Postgres-only** app-state table (PG-first ratchet A3 — Alembic-only
migration, no DuckDB sibling, no `src/db.py` step, `SCHEMA_VERSION` does not
move; pattern of `migrations/versions/0075_corpus_file_sources.py`):

```
sso_config
  id                    TEXT PRIMARY KEY, CHECK (id = 'default')  -- singleton guard
  provider_type         TEXT NOT NULL DEFAULT 'entra_oidc',
                        CHECK (provider_type IN ('entra_oidc'))   -- SAML later widens the CHECK
  tenant_id             TEXT NOT NULL      -- validated via microsoft.tenant_id_error() at the API
  client_id             TEXT NOT NULL
  client_secret_enc     TEXT NULL          -- Fernet token; NULL = not yet set
  display_name          TEXT NOT NULL      -- login button renders "Sign in with {display_name}"
  allowed_email_domains TEXT NOT NULL      -- comma-separated, lowercased/normalized at write
  enabled               BOOLEAN NOT NULL DEFAULT FALSE
  created_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
  updated_by            TEXT NULL          -- admin user id
```

**The client secret is a Fernet-encrypted column on the row itself**, using
the existing shared helpers in `app/secrets_vault.py` (`encrypt_secret` /
`decrypt_optional` under `AGNES_VAULT_KEY`). Neither of the two other
precedents fits:

- `connection_secrets` is a sibling table because `source_connections` is
  many rows and list queries must never SELECT ciphertext. `sso_config` is a
  single row read by one repo — the repo excludes the column from
  `get_config()` and exposes it only through `get_client_secret()`; same
  write-only discipline, one fewer table, one fewer FK lifecycle, and delete
  is atomic.
- `system_secrets` holds env-var-shaped, feature-detached secrets. A
  config-bound secret split from its config row invites the orphan-secret /
  orphan-config bug on delete.

The whole vault posture is inherited: writes without a configured vault key
answer `409 vault_key_not_configured` (the `app/api/admin_slack_secrets.py`
contract); a secret that no longer decrypts (rotated/malformed key) reads as
*unset* via `decrypt_optional` — the provider becomes unavailable, it never
500s.

**Not the YAML overlay.** `/admin/server-config` writes a plaintext overlay
file and masks (does not encrypt) secrets, and its `auth` section is
danger-classified with a restart notice because OAuth clients are built at
boot. DB rows are read live — the reason `source_connections` superseded the
corresponding YAML blocks (`app/api/admin.py` records this) — so SSO config
changes take effect on the next request, no restart, and the secret is
encrypted at rest.

### 3. Identity capture: `user_external_identities` + binding

Second Postgres-only table (same migration):

```
user_external_identities
  user_id        TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE
  provider_type  TEXT NOT NULL
  subject        TEXT NOT NULL          -- Entra `oid` (directory object ID)
  tenant_id      TEXT NOT NULL          -- Entra `tid` at link time
  email_at_link  TEXT NOT NULL          -- asserted email at first link (drift forensics)
  linked_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
  last_login_at  TIMESTAMPTZ NULL
  UNIQUE (provider_type, tenant_id, subject)
```

- `user_id` as PK **is** the "one external identity per user" rule.
- `tenant_id` is part of the uniqueness key because Entra's `oid` is unique
  *within* a tenant; `(tid, oid)` is the globally unique pair. Rows always
  store the **`tid` claim from the validated token** — never the configured
  `tenant_id` string, which may be a verified domain (`fabrikam.com`) while
  the token's `tid` is always the directory GUID. The login lookup keys on
  the token `tid` for the same reason; keying on the configured string would
  miss every row the moment an admin configures by domain instead of GUID.
- Stale bindings (rows whose `(provider_type, tenant_id)` no longer matches
  what the active config's logins assert — e.g. after the admin re-points
  `sso_config` at a different tenant) are unreachable by the lookup and
  **self-heal on the user's next successful login** (see the binding
  algorithm's replacement rule) — no purge required at reconfigure time.
- `subject` stores **`oid`, not the OIDC `sub`**. Entra's `sub` is pairwise
  per app registration — it changes if the customer re-creates the app
  registration, and it correlates with nothing outside this one client.
  `oid` is stable across the tenant's applications and is the ID Microsoft
  Graph reports. (This gets a model docstring so nobody "fixes" it to `sub`.)
- `users` is a frozen DuckDB↔PG pair — it gets **no new columns**. The FK
  from a PG-only table into `users` is fine: the identity table exists only
  where the app-state backend is Postgres.

**Binding algorithm** (in the callback, after the domain check; "this
login's identity" below means `(provider_type='entra_oidc',
tenant_id=<token tid>, subject=<token oid>)` — the **validated token's**
claims, never the configured tenant string):

1. Look up this login's identity. Hit → load the bound user row and check
   `active` **first**: a deactivated account answers the existing
   `deactivated` error before any JWT is issued or `last_login_at` is
   touched. (`ensure_user` performs this check on the attach path, but the
   subject-hit path bypasses `ensure_user` entirely — without the explicit
   check here, a deactivated user would get a fresh cookie and then 401 on
   every request.) Then sign in as `row.user_id`. Subject binding beats
   email: an email change in the customer tenant cannot re-target the login
   onto a different existing Agnes account. Update `last_login_at`; if the
   asserted email differs from the account's email, log the drift at INFO —
   never rewrite either side.
2. Miss → email attach: `ensure_user(email, name,
   source="auth.sso:first-signin")` (the shared provisioning path — JIT
   create, Everyone membership once, deactivated-account rejection), then
   INSERT the identity row. Before the INSERT, check whether that user
   already holds a row:
   - Row from a **different tenant or protocol** (its
     `(provider_type, tenant_id)` ≠ this login's) → it is a stale binding
     from a config that no longer exists (tenant re-point, future protocol
     cutover); the active config's assertions are the current trust anchor,
     so **replace** the row with this login's identity and log the
     replacement at WARNING (`sso.identity.linked` audit row notes the
     replaced subject). Without this rule, every previously-linked user
     would dead-end on the `user_id` PK after an admin re-points the tenant.
   - Row from the **same tenant and protocol** with a **different**
     `subject` → refuse with `/login?error=sso_identity_conflict`. A second
     external principal resolving to the same mailbox must not silently ride
     the first one's account. The canonical way this happens legitimately is
     **email recycling at the customer** (employee leaves, successor gets the
     address, new `oid`): the refusal is correct — the successor must not
     inherit the predecessor's Agnes account — and the recovery is an
     explicit admin decision (unlink the stale identity, or deactivate the
     old account). To make that recovery findable, log at WARNING with the
     user id and both subject GUIDs (no raw emails), and the error page copy
     tells the user to contact their administrator.
   - Race handling: on a unique violation from a concurrent first login,
     re-read by this login's identity and proceed as step 1 if the winner
     matches; otherwise refuse with the same conflict error.

### 4. Exposure: repo lookup, `/api/me`, no JWT claim

- **Primary seam** (for the future data filtering):
  `user_external_identities_repo().get_by_user_id(user_id)` → `{subject,
  tenant_id, email_at_link, linked_at, last_login_at} | None`. Every
  authenticated code path holds the principal's `user["id"]`; resolving the
  external identity is one live PK read.
- **User-visible surface:** `GET /api/me/external-identity` returning
  `{linked, provider_type, tenant_id, subject, linked_at, last_login_at}`
  (none of these are secrets), rendered as a "Linked identity" line on the
  profile page.
- **No JWT claim.** (i) The session JWT is documented identity-only
  (`sub`/`email` + metadata) precisely so authorization-adjacent facts stay
  revocable; a claim would snapshot link state into a 30-day stateless token
  with no kill switch — an admin unlinking a mis-attached identity would
  keep leaking the old `oid` into consumers for up to 30 days. (ii) PATs and
  CLI tokens would not carry it, so every consumer needs the DB path anyway;
  a claim adds a second, staler source of truth. (iii)
  `create_access_token()` already supports `extra_claims`, so if a measured
  hot path ever justifies a claim it is a two-line change later — the
  reverse (recalling issued claims) is impossible. (iv) The eventual
  consumer is security-relevant filtering; security-relevant lookups should
  be fresh. If a per-request consumer appears, cache on `request.state`,
  never in the token.

## Trust model

This section is load-bearing; the operator doc repeats it.

**The external tenant's admin can assert any email.** Entra (like any IdP)
puts whatever email the tenant admin configures into the token. On
first-ever SSO login Agnes attaches by email (`ensure_user` →
`get_by_email_ci`) — so whoever controls the customer tenant can, for any
email in the *permitted domains*, attach to (or create) the matching Agnes
account. Subject pinning (piece 3) protects accounts **after** they link; the
domain allowlist is the boundary **before**.

**Therefore the domain allowlist is mandatory and must contain only domains
whose identities the external tenant is entitled to assert** — the
customer's own domains (`fabrikam.com`), never the operator's
(`example.com`). Scoped that way, the customer tenant's admin can
impersonate only users they already own. This is the same trust statement
every IdP integration makes (a Google Workspace admin can do the identical
thing to `google`-provider users today; the `microsoft` module's docstring
already frames tenant ≠ identity boundary) — what is new here is that the
tenant belongs to a **third party**, which is why the domain scoping is the
whole game. Design consequences:

- `allowed_email_domains` is **required and explicit**. It does *not*
  inherit the instance-level `auth.allowed_domain` — inheritance would
  silently extend the customer tenant's assertion power over operator
  accounts whenever the instance list contains the operator's domain.
  Enabling (or running) the provider with an empty list is refused.
- The check runs in the callback **before** JIT user creation, fail-closed
  (empty list = deny all), on the resolved identity's domain, lowercased —
  same mechanics as the `microsoft` provider's `allowed_domain` gate.
- **B2B guests**: guests invited into the customer tenant authenticate too,
  and their `email` claim carries their external address. Tenant pinning
  does not stop them — the domain allowlist does. (Same posture as
  `microsoft.py`; its `#EXT#` UPN refusal is documented as *not* a guest
  guard.)

**Coexistence of logins is deliberate.** A user who links an external
identity keeps every other way in (password, Google, magic link — whatever
the instance offers). This is the requirement ("current login + the new
external login"), and it is the opposite of the sibling product's
SSO-enforcement model (where a user's first SSO login permanently disables
their password). Trade-off stated plainly: a phished password still works
after the user links SSO; the external IdP's MFA/conditional-access posture
protects only the SSO door. Login-method enforcement is a possible future
switch, out of scope here.

**Accepted risks (v1), documented rather than mitigated:**

- First-login email attach itself — deliberate, same semantics as every
  Agnes provider.
- No session revocation on unlink or disable: sessions are 30-day stateless
  JWTs; the existing platform posture (`users.active` is the kill switch)
  applies unchanged.
- The operator must trust the customer tenant's hygiene *within the
  permitted domains* (offboarding, guest curation). The audit trail
  (`sso.identity.linked` rows, `email_at_link`, `last_login_at`) is the
  detective control.

## Configuration & lifecycle

**Admin API** (`app/api/admin_sso.py`, all `Depends(require_admin)`, all
mutations audited with secret-free params):

| Route | Behavior |
|---|---|
| `GET /api/admin/sso/config` | `{configured, enabled, provider_type, tenant_id, client_id, display_name, allowed_email_domains, has_client_secret, vault_key_configured, updated_at, updated_by}` — never the secret |
| `PUT /api/admin/sso/config` | Upsert. Validates `tenant_id` via imported `tenant_id_error()` (refuses `common`/`organizations`/`consumers` + consumer GUIDs; GUID or verified domain only), non-empty `display_name`, normalized non-empty `allowed_email_domains`. Enable guard: `enabled=true` requires a stored, decryptable secret and a non-empty domain list → else 422 |
| `PUT /api/admin/sso/client-secret` | Write-only; `409 vault_key_not_configured` without a vault key (clones the slack-secrets contract) |
| `DELETE /api/admin/sso/client-secret` | Clears the secret (provider becomes unavailable) |
| `DELETE /api/admin/sso/config` | Deletes the row + secret. **Keeps** `user_external_identities` rows: they are historically true links, inert unless the same tenant is configured again; purgeable per-user via the identities endpoint |
| `POST /api/admin/sso/test-config` | Server-side probe: tenant validation, secret present + decryptable, then fetches `https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration` and reports issuer/endpoints. No SSRF surface: fixed host, tenant validated and `quote()`d into a single path segment (the `microsoft.py` discovery-URL pattern) |
| `GET /api/admin/sso/identities` | List linked identities (user, subject, tenant, linked_at, last_login_at), **paginated**: `limit` (default 50, max 200) + `offset`, ordered `linked_at` DESC, response carries `total`. The repo has no unbounded list method — one admin call must not serialize an entire tenant's links |
| `DELETE /api/admin/sso/identities/{user_id}` | Admin unlink — hard delete; the recovery tool for a mis-attach. Does not touch the user row |

Audit event names: `sso.config.set`, `sso.config.enable`,
`sso.config.disable`, `sso.config.delete`, `sso.secret.set`,
`sso.secret.delete`, `sso.identity.linked` (from the callback),
`sso.identity.unlinked`.

**Admin test sign-in** (end-to-end proof before the button goes live):
`GET /auth/sso/login?mode=test` requires an **admin** session and works
while `enabled=false` (gated on config-completeness, not on enabled, and
independent of the `auth.providers` allowlist — the inline-gating rule in
Scope piece 1 exists precisely so this path stays reachable pre-launch). It
stashes a test marker in the session; the callback, seeing the marker and
re-verifying the *current session user* is an admin, completes the code
exchange and renders a result page — resolved email, `oid`, `tid`, the
domain-allowlist verdict, and which Agnes account the login *would* attach
to — **without** calling `ensure_user`, without writing an identity row,
and without setting a cookie. This is the difference between "config saved"
and "login proven" against a third party's tenant the operator cannot see
into (redirect URI registered? secret valid? claims present?).

**Enablement** is then just `enabled=true` on the config PUT — instant on
the login page, instantly reversible. The provider reads config live:
`is_configured()` (row complete + secret decryptable) vs `is_available()`
(Postgres backend + configured + enabled). The authlib client follows the
`keboola.py::_oauth_client()` pattern — fingerprint
`(tenant_id, client_id, client_secret)`, evict and re-register on change —
so secret rotation and tenant re-pointing take effect on the next login
attempt with no restart.

**Last-login-door guard.** Three transitions can turn a live login door
off: `PUT` with `enabled=false`, `DELETE …/client-secret`, and `DELETE
…/config`. Each is refused with a 422 (`last_login_door`) when the
post-operation provider set would leave **no usable sign-in method** — the
same no-lockout rule `/admin/server-config` already applies to
`auth.providers` edits via `_validate_auth_providers_in_patch`, evaluated
with the same probe logic. Without it, an instance running
`auth.providers: [sso]` (and no usable password/email fallback) would lock
itself out the moment the admin disables SSO, once current sessions expire.
The break-glass path is documented rather than built: `AGNES_AUTH_PROVIDERS`
(env wins over instance.yaml) plus the registry's password/email rescue
already let an operator with server access reopen a door.

**DuckDB-backed instances fail clean.** The two repos are PG-only; their
factories raise `RequiresPostgresBackend` → the app-wide handler answers a
typed 501 on the admin routes (with the parity-sweep exemptions proving it).
The provider's availability probe checks the backend **first** and returns
`False` — it must never let `RequiresPostgresBackend` escape, because a
raising probe would suppress the provider-registry lockout rescue and spam
warnings on every login render of a DuckDB instance.

## Login flow

Routes shaped like the existing providers (gating is the inline rule from
Scope piece 1 — normal mode matches today's 404 posture, test mode is
admin-only):

```
GET /auth/sso/login      302 → Entra authorize (authlib mints state+nonce in the
                         Starlette session; ?next= stashed via safe_next_path;
                         prompt=select_account forced)
GET /auth/sso/callback   code exchange → claims → identity → bind → JWT cookie
```

`prompt=select_account` is passed on the authorize redirect (a deliberate
difference from the `microsoft` provider): users at a customer commonly hold
several active Entra sessions, and without the account picker Entra may
silently SSO an already-signed-in identity — e.g. a B2B guest session —
which then dies on the domain allowlist with no chance to pick the right
account.

Callback ordering:

1. `authorize_access_token()` (authlib validates `state`, `nonce`, and the
   ID token's `iss`/`aud`/`exp` against the tenant-pinned discovery
   metadata); claims from `token["userinfo"]` — authlib passes through
   Entra's non-standard `oid`/`tid` claims.
2. `resolve_identity()` (imported from `microsoft`): `email` claim first,
   `preferred_username` only as the narrowed non-`#EXT#` fallback; missing →
   `sso_no_email`.
3. Missing `oid` → `sso_no_subject` (never fall back to `sub`).
4. Defensive tenant check: when the configured tenant is a GUID and the
   token's `tid` differs → `sso_wrong_tenant`. Belt-and-braces — the
   tenant-pinned issuer already enforces this via authlib's `iss`
   validation; for verified-domain config the issuer check is the authority.
   In every case the **token's `tid`** (not the configured string) is what
   the binding lookup and the stored row use.
5. Domain allowlist check (before any DB write), lowercased, fail-closed →
   reuses the existing `domain_not_allowed` error copy.
6. Binding algorithm (Scope piece 3). `ensure_user` raising
   `UserDeactivatedError` → the existing `deactivated` error.
7. JWT + cookie issuance byte-for-byte like `microsoft.py`:
   `create_access_token(user_id, email)`, `access_token` httpOnly cookie,
   `samesite=lax`, `cookie_secure(request)`, `session_cookie_domain()`,
   redirect to the `safe_next_path`-sanitized target.
8. Exception handler copies the `%r` + 500-char-slice logging discipline
   (authlib builds `OAuthError` from attacker-controlled query params
   before state validation) → `sso_oauth_failed`.

New `/login?error=` codes needing copy in `login.html`'s `_err_messages`:
`sso_not_configured`, `sso_oauth_failed`, `sso_no_email`, `sso_no_subject`,
`sso_wrong_tenant`, `sso_identity_conflict` (existing `domain_not_allowed`
and `deactivated` are reused).

**CLI and desktop are untouched.** `agnes login` opens the browser at
`/cli/auth/start`, which bounces an unauthenticated user to `/login?next=…`;
any provider button completes the flow, and the PAT mint that follows is
provider-agnostic. The desktop app shells out to the same CLI. The only
obligation on the new provider is the `?next=` stash/pop it already copies
from `microsoft.py`. The terminal-only `agnes login --password` path stays
password-account-only; an SSO-only account gets the existing "signs in via
browser flow" message.

## Surfaces

Adding a provider touches a known set of enumeration points; this design
adds one small guard so they cannot drift (see Testing):

- `app/auth/provider_registry.py` — `KNOWN_PROVIDERS` + `_AVAILABILITY_PROBES`
  entries; `"sso"` appended **last** in `_other_login_door_usable`'s tuple so
  env-configured providers short-circuit before the DB probe runs.
- `app/web/router.py` `login_page` — sixth availability block; button text
  `Sign in with {display_name}`, `btn-primary`.
- `app/api/admin.py` `_provider_available_after_save` — an `sso` branch
  returning the provider's live `is_available()`. Correct because SSO config
  lives in the DB, not in the instance.yaml patch under validation, so
  "current availability == availability after save" (the same argument the
  `microsoft` branch makes for env vars). Without the branch, an
  `auth.providers: [sso]` allowlist would always be refused as "no usable
  sign-in method". Plus a note string pointing the operator at the SSO admin
  panel instead of env vars.
- `app/services/instance_doctor.py` `check_login_door` — add `sso` to the
  provider tuple so a configured+enabled SSO row counts as a login door.
- `app/web/templates/admin_server_config.html` — a bespoke SSO panel next to
  the Slack-secrets panel (config form, secret field with write-only
  semantics, status badges from the GET shape, test-config button, test
  sign-in link, enable toggle carrying the containment-rule copy). Design
  system rules apply (`ds.*` macros, `--ds-*` tokens only).
- Profile page — "Linked identity" line fed by
  `GET /api/me/external-identity`.
- Startup warnings (lifespan pattern): enabled → one line naming the trusted
  tenant and permitted domains; enabled-but-secret-undecryptable → loud
  error.
- **CLI (full parity — every new `/api/*` endpoint is CLI-reachable per
  CONTRIBUTING's API-coverage rule):** an `agnes admin sso` command group in
  `cli/commands/`:
  - `status [--json]` → GET config (never the secret),
  - `set [--tenant-id … --client-id … --display-name … --domains … --enable/--disable]` → PUT config,
  - `set-secret` → PUT client-secret, secret read via **hidden prompt**
    (`typer.prompt(..., hide_input=True)`, the `admin_connection.py`
    precedent — never argv),
  - `clear-secret` / `delete` → the two DELETEs,
  - `test` → POST test-config,
  - `identities [--limit --offset --json]` → GET identities,
  - `unlink <user-id>` → DELETE identity.
  State-changing commands get parity cases in `tests/test_cli_api_parity.py`.
  `GET /api/me/external-identity` is CLI-reachable through `agnes whoami`
  (a "Linked identity" line + `--json` field).
- **MCP: none of these routes is MCP-exposed.** Classification for the
  triple-surface ratchet (`tests/test_documentation_api_triple_surface.py`):
  config/secret writes and `test-config` fall under the standing
  **admin credential-provisioning writes** exemption (they reconfigure which
  upstream a credential authenticates against); `GET …/config` and `agnes
  whoami`'s identity read fall under the standing **operator
  security-posture diagnostics** exemption (they enumerate the instance's
  auth posture / the caller's auth linkage); `identities` list + `unlink`
  are `_EXEMPT` with their own one-liner — unlink re-opens email-attach for
  that user (an auth-trust mutation), and neither is analyst tooling.

## Operator ↔ customer exchange checklist

Goes into `docs/auth-sso-entra.md` (operator doc, sibling of
`docs/auth-microsoft-oauth.md`); mirrored here because it is part of the
design.

**Agnes admin → customer IdP admin** ("please register an app for us"):

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
```

**Customer IdP admin → Agnes admin** (entered into the admin UI):

- Directory (tenant) ID — GUID from the app registration's Overview page
  (a verified domain like `fabrikam.onmicrosoft.com` also works)
- Application (client) ID
- Client secret **value** + its expiry date (calendar the rotation —
  an expired secret turns the button into `sso_oauth_failed`)
- The email domain(s) to permit (the customer's own domains only)

## Decisions taken

- OIDC/Entra now; SAML deferred behind the `provider_type` discriminator —
  no SAML code, schema, or native dependency in v1.
- New `sso` provider slot; `microsoft` stays env-only and untouched except
  for importing its pure helpers.
- Singleton `sso_config` with `CHECK (id='default')`; going multi-row later
  is one migration dropping the CHECK.
- Client secret = Fernet column on the config row (not `system_secrets`, not
  a sibling table); write-only API; vault-key 409 contract.
- `allowed_email_domains` is required and explicit — **no inheritance** from
  instance `auth.allowed_domain`; empty ⇒ cannot enable, and the callback
  fails closed regardless.
- Identity row stores `oid` (+`tid`), never `sub`; uniqueness is
  `(provider_type, tenant_id, subject)`; `user_id` PK caps one external
  identity per user.
- Binding is keyed on the **validated token's** `tid`/`oid`, never the
  configured tenant string (which may be a verified domain while `tid` is
  always the GUID).
- Subject binding beats email attach; email drift is logged, never
  auto-rewritten. A stale row from a no-longer-configured tenant/protocol is
  replaced on the next successful login through the active config; a
  same-tenant conflicting subject (e.g. customer-side email recycling)
  refuses the login with admin-findable WARNING diagnostics.
- `prompt=select_account` on the authorize redirect (multi-session users and
  B2B guests must get the account picker, not a silent SSO into a
  domain-check failure).
- `oid` exposure via live repo lookup and `/api/me/external-identity`; **no
  JWT claim**.
- JIT-created SSO users get Everyone membership only — no group sync from
  the external tenant, no per-config default group (matches the `microsoft`
  provider's posture; a default-group column is a cheap later addition if
  demand appears).
- Config delete keeps identity rows (inert by uniqueness-key design); admin
  unlink is a hard per-user delete.
- Both logins coexist; no SSO enforcement.
- Admin test sign-in ships in v1 (side-effect-free, admin-gated, works
  pre-enable and independent of the allowlist — hence inline route gating
  instead of the router-level `require_provider` dependency).
- Disable / clear-secret / delete-config are guarded by the same
  no-last-login-door rule the server-config allowlist editor applies;
  break-glass is the documented `AGNES_AUTH_PROVIDERS` env override.
- Full CLI parity (`agnes admin sso …` group, hidden-prompt secret entry,
  `agnes whoami` carries the linked identity); **no MCP exposure** for any
  of these routes, classified via the standing CONTRIBUTING exemptions.
- Everything lands as **one PR** (schema → API → provider → UI → docs as
  internal work order, not separate PRs).

## Security checklist

Against `.claude/skills/agnes-conventions/references/security.md`:

- **Secrets:** never on argv or in URLs (web-only writes, POST bodies);
  Fernet at rest; write-only API (`has_client_secret`, never the value);
  audit rows carry no values; logs never print the secret, tokens, or raw
  OAuth error text unescaped (`%r` + slice).
- **CSRF/state:** OAuth `state`+`nonce` via authlib in the server-side
  session (SessionMiddleware). Admin mutations are JSON `PUT`/`POST`/`DELETE`
  on bearer/cookie-authenticated `/api/*` routes — same posture as the
  existing slack-secrets admin API. The test sign-in result page performs no
  mutation.
- **Open redirect:** `?next=` through `safe_next_path` on both ends, exactly
  like `microsoft.py`.
- **SSRF:** the only server-side fetch (`test-config`, discovery document)
  targets a fixed host with the tenant validated by `tenant_id_error()` and
  `quote()`d into one path segment.
- **Injection:** no SQL built from claims (repos use bound parameters);
  `display_name` is template-escaped by Jinja autoescape on the login page.
- **DoS/regex:** identity/domain checks reuse the linear-time helpers from
  `microsoft.py`; no new regex over untrusted text.
- **RBAC:** all admin routes `Depends(require_admin)`; test sign-in
  re-verifies the session user is admin inside the callback; per-request
  provider gating via the inline rule (normal mode: allowlist + availability
  → 404, matching `require_provider` semantics; test mode: admin-only).
- **Rate limiting:** `/auth/sso/*` is covered by the existing auth rate-limit
  middleware on `/auth/*`.
- **Availability probes never raise** on DuckDB backends (typed-501 stays an
  admin-API-only behavior).

## Testing

House pattern (`tests/test_auth_providers.py`): pure-function unit tests plus
monkeypatching of module state; no live-IdP E2E (none exists for any
provider).

- `tests/db_pg/test_sso_contract.py` — repo contract tests: singleton CHECK,
  unique-violation shapes, secret roundtrip under a test vault key,
  decrypt-failure-reads-as-unset, identity link/unlink/touch.
- `tests/test_admin_sso_api.py` — admin gate; validation matrix (reserved
  tenants via the imported validator, empty display name, empty domains,
  enable-without-secret, enable-with-empty-domains → 422); secret write-only
  invariants; vault 409; audit rows written; config-delete keeps identities;
  **last-login-door guard** (disable/clear-secret/delete-config refused with
  422 when SSO is the only usable door, allowed when another door is
  usable); identities pagination (limit clamped to the max, offset past the
  end, `total` correctness).
- Provider tests in the `test_auth_providers.py` style — availability truth
  table (DuckDB → False-not-raise, disabled, secret missing, decrypt fail);
  binding algorithm (subject-hit beats email, **subject-hit on a deactivated
  account → `deactivated`, no JWT, `last_login_at` untouched**, email-attach
  + link, same-tenant conflict refusal + WARNING diagnostics, stale-row
  replacement after a tenant re-point, token-`tid` keying under a
  verified-domain configured tenant, race via forced unique violation,
  domain check before JIT, deactivated on attach); allowlist gating (normal
  mode 404s when `sso` is excluded, `[sso]` 404s the others).
- Test-mode security and reachability: non-admin start refused; marker
  without an admin session refused; asserts **no** user row, no identity
  row, no cookie; and test mode still works with `sso` absent from an
  explicit `auth.providers` list and with `enabled=false` (the inline-gating
  rule).
- A small registry-parity guard asserting `login_page`, `probe_providers`,
  the doctor, and `_provider_available_after_save` all agree on the provider
  set (the enumeration now lives in five places — cheap drift insurance).
- Existing ratchet guards cover the rest automatically
  (`test_repository_registry*`, `test_repo_module_pg_first_ratchet`,
  `test_backend_split_guard`, parity sweeps + the new
  `_PG_ONLY_ROUTE_EXEMPTIONS` entries with clean-501 assertions, OpenAPI
  snapshot refresh, design-system contract).

## Implementation plan — one PR, five internal steps

Single PR; the steps are work order and review structure, not separate PRs.
CHANGELOG bullet (Added) included in that PR.

1. **Schema + repositories.** `migrations/versions/0077_sso_login.py` (both
   tables, 0075-style A3 docstring); `src/models/sso.py` (+`__init__`
   registration); `src/repositories/sso_config_pg.py` (`get_config` excludes
   secret, `upsert_config`, `set_client_secret`, `get_client_secret`,
   `clear_client_secret`, `set_enabled`, `delete_config`);
   `src/repositories/user_external_identities_pg.py` (`get_by_user_id`,
   `get_by_subject`, `link` with conflict report, `touch_last_login`,
   `unlink`, `list_page(limit, offset)`, `count` — no unbounded list);
   PG-only `_REGISTRY` entries + factories in
   `src/repositories/__init__.py`; contract tests.
2. **Admin config API.** `app/api/admin_sso.py` (routes per the lifecycle
   table); router registration in `app/main.py`; parity-sweep exemptions;
   API tests; OpenAPI snapshot.
3. **Provider + login flow + binding.** `app/auth/providers/sso.py`
   (runtime client per `keboola._oauth_client` fingerprint pattern, callback
   per the Login-flow section, `startup_warnings()`); registry entries +
   `_other_login_door_usable`; `app/main.py` router + startup-warning wiring;
   `login_page` block + `login.html` error copy; `_provider_available_after_save`
   branch; doctor tuple; provider/binding/allowlist tests + registry-parity
   guard.
4. **Surfaces.** Admin panel in `admin_server_config.html`; test sign-in
   (`?mode=test` + result template); `GET /api/me/external-identity` +
   profile line; surface tests (incl. test-mode security) and design-system
   contract.
5. **Docs + CLI + polish.** `docs/auth-sso-entra.md` (trust model +
   exchange checklist); cross-link from `docs/auth-microsoft-oauth.md`;
   the `agnes admin sso` command group (status/set/set-secret/clear-secret/
   delete/test/identities/unlink per the Surfaces section) + the
   `agnes whoami` linked-identity line; `tests/test_cli_api_parity.py`
   cases for the state-changing commands; triple-surface ratchet
   classifications (`_EXEMPT` entries); CHANGELOG bullet.

## Out of scope

- SAML (documented future option behind `provider_type`).
- The SharePoint/Graph data filtering itself — this design only guarantees
  the `oid`/`tid` are captured and resolvable server-side.
- Group sync from the external tenant (same standing TODO as the `microsoft`
  provider); per-config default groups.
- Multiple external IdPs per instance.
- SSO enforcement / login-method lockout.
- Self-service unlink (admin-only in v1).
- Session revocation on unlink/disable (stateless-JWT platform posture).
- MCP exposure of the SSO admin/identity routes (classified `_EXEMPT`; see
  Surfaces).
