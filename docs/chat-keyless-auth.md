# Chat: keyless LLM auth (Workload Identity Federation)

By default the chat broker authenticates to Anthropic with a static
`ANTHROPIC_API_KEY`. **Keyless mode** replaces that key with a **short-lived
token minted from the workload's own OIDC identity**, via Anthropic's native
Workload Identity Federation (WIF). There is no long-lived key to store, fund,
rotate, or leak.

This is **opt-in**; `api_key` mode stays the default. Nothing changes for
installs that don't enable it.

- Design/rationale: [`docs/superpowers/specs/2026-07-15-keyless-llm-auth-design.md`](superpowers/specs/2026-07-15-keyless-llm-auth-design.md)
- Anthropic reference: <https://platform.claude.com/docs/en/manage-claude/workload-identity-federation>

## How it works

```
chat sandbox ──(dummy key)──▶ in-sandbox relay ──(ticket)──▶ broker ──(federated
                                                              │           Bearer)──▶ api.anthropic.com
                                                              └─ mints the token server-side from the
                                                                 workload's OIDC identity (never in the sandbox)
```

Only the broker's credential injection changes: instead of `x-api-key: <static>`
it sends `Authorization: Bearer <federated token>` (plus the
`anthropic-beta: oauth-2025-04-20` header OAuth-style tokens require). The token
is minted server-side and cached/refreshed. The sandbox still holds **no**
credential of any kind — the same isolation as `api_key` mode, minus the durable
key.

## Prerequisites (operator, one-time)

1. **An Anthropic federation rule** (Anthropic Console → *Workload Identity
   Federation*). It ties a trusted OIDC **issuer** + **audience** (and optional
   `subject`/`claims` matchers) to an Anthropic **service account** and an
   **OAuth scope** (`workspace:inference` is enough for chat — it grants the
   Messages/Models endpoints). Note the resulting ids:
   - federation rule id `fdrl_…`
   - organization id (UUID, Console → *Settings → Organization*)
   - service account id `svac_…`
   - workspace id `wrkspc_…` (only needed if the rule spans multiple workspaces)
2. **An OIDC identity token source on the workload** — a signed JWT proving the
   workload's identity, minted for the rule's audience. Any OIDC provider works;
   common sources:
   - a cloud VM/serverless **metadata identity endpoint** (writes/serves a JWT
     for a requested audience),
   - a **projected service-account token file** (e.g. Kubernetes),
   - any file/env your platform can populate.

   The audience the token is minted for **must match** the federation rule's
   `audience`.

## Enable it

1. **Set the federation environment** on the Agnes server process (the same env
   contract the Anthropic SDK uses):

   | Variable | Required | Value |
   |---|---|---|
   | `ANTHROPIC_FEDERATION_RULE_ID` | yes | `fdrl_…` |
   | `ANTHROPIC_ORGANIZATION_ID` | yes | org UUID |
   | `ANTHROPIC_SERVICE_ACCOUNT_ID` | yes | `svac_…` |
   | `ANTHROPIC_IDENTITY_TOKEN_FILE` **or** `ANTHROPIC_IDENTITY_TOKEN` | yes (one) | path to the JWT / the JWT itself |
   | `ANTHROPIC_WORKSPACE_ID` | conditional | `wrkspc_…` or `default` — only when the rule spans multiple workspaces |

   Prefer `ANTHROPIC_IDENTITY_TOKEN_FILE` for rotating projected tokens — the
   server re-reads the file on every exchange.

2. **Unset the static credential.** `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`
   **outrank** federation — if either is set (even to an empty string) the
   federation path never activates. `unset` them; do not blank them.

3. **Switch the config** in `instance.yaml`:

   ```yaml
   chat:
     enabled: true
     llm:
       auth: workload_identity   # default is api_key
   ```

4. **Restart** the server.

## Verify

- **Startup gate:** in `workload_identity` mode the server validates the
  federation env at boot. If it's incomplete it refuses to start chat with a
  clear log (`chat.llm.auth=workload_identity requires the federation env … missing: …`)
  rather than failing later on the first completion.
- **Admin "test connection":** `/admin` → chat secrets → *Test* (or
  `POST /api/admin/chat/secrets/test`). In keyless mode the `anthropic_api_key`
  probe mints a federated token and runs a 1-token completion, returning
  `{ok, detail}` — the keyless analog of the static-key test.
- **A real chat turn** should complete normally; nothing about model ids,
  streaming, or tools changes.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Chat disabled at startup, log says the federation env is missing | one of the four required vars unset | set all of `ANTHROPIC_FEDERATION_RULE_ID` / `ANTHROPIC_ORGANIZATION_ID` / `ANTHROPIC_SERVICE_ACCOUNT_ID` / `ANTHROPIC_IDENTITY_TOKEN[_FILE]` |
| Broker returns `502 workload_identity token exchange failed` | the OIDC JWT was rejected at exchange (`invalid_grant`) | check the [WIF auth history](https://platform.claude.com/settings/workload-identity-federation?tab=history); most often the token's `aud` ≠ the rule's `audience`, or `iss` ≠ the registered issuer, or the token expired |
| Admin test says "federated token minted but API call failed" | token was minted but the API rejected it | the rule's `oauth_scope` may not cover Messages (use `workspace:inference` or `workspace:developer`); a `403` means out-of-scope |
| It still uses the old key / behaves like `api_key` mode | `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` still set | `unset` them (an empty string still wins its precedence slot) |

## Notes

- **Vendor-neutral:** keyless is provider-agnostic — any OIDC identity source
  works. Cloud-specific federation-rule and identity-token wiring belongs in the
  deployment/infra configuration, not in this repo.
- **Security:** the federated token lives only on the server (broker), never in
  the sandbox, and is short-lived (default ~1 h, capped by the rule's
  `token_lifetime_seconds`). The chat sandbox secret-broker isolation is
  preserved and strengthened — there is no durable key anywhere.
- **On GCP, `chat.llm.provider: vertex` is the other keyless option:** instead
  of federating to the first-party Anthropic API, the broker signs requests
  with the workload's ambient Google identity (ADC) and forwards to Claude on
  Vertex AI — see `docs/cloud-chat.md` → "LLM provider: Google Vertex AI".
  The two modes are mutually exclusive (boot refuses
  `provider: vertex` + `auth: workload_identity`); pick the one matching where
  your Claude capacity is provisioned.
