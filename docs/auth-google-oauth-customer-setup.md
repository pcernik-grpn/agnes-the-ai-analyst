# Google sign-in registered by the customer — what to exchange

Use this shape when the people signing in belong to a **customer's** Google
Workspace rather than the operator's own: the customer registers the OAuth
client in their own Google Cloud project, keeps its consent screen
**Internal**, and hands over the client id + secret. Google then guarantees
that only that Workspace can pass the consent screen at all, which makes
`auth.allowed_domain` a second, narrower gate instead of the only one.

The alternative — the operator's own client with an **External** consent
screen and the customer's domains in `auth.allowed_domain` — is in
[`auth-google-oauth.md`](auth-google-oauth.md) → *Audience: who the client
admits*. Pick one: an instance carries exactly one Google client.

**One Google door per instance.** `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`
is a single client, so handing the Google slot to the customer means the
operator's own staff sign in through a different provider. `password` is the
usual answer — it never checks `auth.allowed_domain`, so an operator account
stays reachable on an instance whose allowlist names only customer domains
(the same asymmetry `POST /api/users` warns about rather than refuses). Give
at least one customer-domain admin a password too: that is the break-glass
door for the day the customer's client is rotated, re-pointed, or deleted.

## Ask the customer's Google admin for this

```
OAuth client for Agnes single sign-on
=====================================
Cloud project:   a Google Cloud project in YOUR organization. A Workspace
                 domain on its own is not enough — the OAuth client lives in
                 a Cloud project, and it is the project's parent
                 organization that decides who "Internal" means
Audience:        Internal  (Google Auth Platform -> Audience)
                 Internal admits only accounts in the Workspace organization
                 that owns the project. It needs no Google verification, has
                 no test-user cap, and never has to be published
Client type:     Web application  (Credentials -> Create credentials ->
                 OAuth client ID)
Redirect URI:    https://<agnes-host>/auth/google/callback
                 (one line per hostname the instance answers on — add the
                 future hostname too if a rename is planned)
Scopes:          openid email profile — nothing else. All three are
                 non-sensitive, so no verification review is involved
Branding:        app name, user support email, developer contact. An
                 Internal consent screen does not list scopes
API controls:    if Security -> Access and data control -> API controls
                 blocks unconfigured third-party apps, allow this app (or
                 tick "Allow access to third-party apps that only require
                 Google sign-in"). A blanket block refuses sign-in scopes
                 too, and surfaces as Error 400: admin_policy_enforced
```

## What comes back

- **Client ID** — public, safe to paste anywhere.
- **Client secret** — over a secret channel; it goes straight into the
  deployment's secret store, never into `instance.yaml` in a repo.
- **The exact email domains their users sign in with.** This becomes
  `auth.allowed_domain`. Derive it from the domains, not from the company's
  website: an address on a domain nobody listed is refused as
  `domain_not_allowed`, and a domain listed by mistake self-provisions an
  account on first sign-in.

**Settle the multi-domain question before the client is created.** Internal is
scoped to the Workspace *account*, not to a domain name — several email
domains pass only when they are secondary domains of that same account. A
group that runs a separate Workspace tenant per company cannot be served by
one Internal client, and there is no second Google slot to give the other
tenants. That case needs an External client (theirs or the operator's) with
every domain in `auth.allowed_domain`, and the allowlist is then the whole
boundary. Ask which shape the customer has.

## Operator side

1. **Store both values** where the deployment reads them —
   `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`. The bundled Terraform module
   reads them from Secret Manager under `google-oauth-client-id` /
   `google-oauth-client-secret`, or the per-VM names expanded from
   `oauth_secret_name_template`.
2. **`auth.allowed_domain`** = the customer's domains as a comma-separated
   **string** (a YAML list raises inside the callback and is swallowed as
   `error=oauth_failed`). Leave the operator's own domain out — it reaches the
   instance through the password door, and keeping it out of the allowlist
   keeps the Google door strictly the customer's.
3. **`auth.providers: [google, password]`** so both doors are offered. Every
   allowed provider renders on the login page, so customer users will see the
   password form as well; point them at the Google button in onboarding.
4. **Restart the app** — the provider captures the two env vars at module
   import. Under the Terraform module the startup script rewrites `.env` on
   every boot, so a reboot is enough; no instance replacement, and
   `deletion_protection` can stay on.

## Verify

```bash
docker compose exec app env | grep -c GOOGLE_CLIENT   # expect 2
```

Then walk all three refusals, because they fail in different places:

| Attempt | Expected |
|---|---|
| A customer-domain account | signs in, account self-provisions |
| An account on a domain outside `auth.allowed_domain`, inside their Workspace | `/login?error=domain_not_allowed` — Agnes refused it |
| Any Google account outside their Workspace | Google's own `org_internal` error, before Agnes sees the request |

## Ongoing ownership

- **The client is theirs.** They can rotate the secret, change the audience,
  or delete it; the first symptom is `/login?error=oauth_failed` for every
  user, with the real reason only in the app log. Google client secrets do not
  expire on their own (unlike Entra's), so there is no rotation date to
  calendar — but a rotation on their side is an outage until the new value is
  stored and the app restarted. Say so in the handover, and keep the
  break-glass password account from the top of this page.
- **Group sync is independent of who owns the client.** It authenticates as
  the deployment's own service account through domain-wide delegation in the
  customer's Workspace ([`auth-groups.md`](auth-groups.md)) and needs a super
  admin there. Without it a signed-in customer user simply has no groups, and
  RBAC is admin-assigned at `/admin/access`. Do not set
  `AGNES_GOOGLE_GROUP_PREFIX` unless the prefix gate is meant — it turns
  group membership into a login condition.
