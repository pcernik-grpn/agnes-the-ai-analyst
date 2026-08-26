# Seed repo contract

Audience: anyone building or forking a workspace seed repo for an Agnes
deployment. For the operator-side configuration (registering a seed via
the admin UI, sync semantics, override behavior), see
[`docs/initial-workspace-override.md`](initial-workspace-override.md).

> The OSS reference seed lives at
> [`github.com/keboola/agnes-infra-template`](https://github.com/keboola/agnes-infra-template)
> (the same repo that ships the Terraform skeleton — its `workspace/`,
> `.claude-plugin/`, `plugins/`, and `install-prompt/` sub-trees are
> the canonical seed content) and ships pre-bundled inside every
> Agnes wheel at `src/_bundled_seed/`. When the operator hasn't
> configured an Initial Workspace Template, Agnes falls back to that
> bundle so the install prompt + canonical connectors always render.
> Forks replace the bundle by running `scripts/sync_bundled_seed.sh`
> against their own repo.

---

## 1. What is a seed repo

A seed repo carries the contents Agnes ships to analysts on `agnes init`.
The Agnes server clones it, validates the tree, and serves it as a zip
back to each analyst CLI. Beyond the file payload, the seed also drives
two server-side render paths:

- The **install-prompt** rendered on `/home` (the "paste-into-Claude-Code"
  bootstrap script).
- The **connector manifest** behind the post-install connector listing
  (`required: true` entries first, then the optional ones).
- The **per-connector wizard bodies**, served on demand by
  `GET /api/connectors/{slug}/prompt` rather than inlined anywhere.

When an Initial Workspace Template is registered, the operator's seed
beats the bundled snapshot tier-by-tier (the server reads the IWT clone
first for each file). When no IWT is registered, the bundle wins.

---

## 2. Repo layout contract

```
<seed-root>/
├── workspace/                                # SHIPPED to analysts (existing IWT contract)
│   ├── CLAUDE.md                             # optional — agent workspace prompt
│   ├── AGNES_WORKSPACE.md                    # optional — analyst-facing doc
│   ├── .claude/
│   │   ├── settings.json                     # hooks + perms
│   │   ├── skills/
│   │   │   ├── connector-<slug>/SKILL.md     # connector wizards (frontmatter contract §4)
│   │   │   ├── _lib/CONNECTOR_USAGE.md       # optional — author guide
│   │   │   └── ...                           # any other skills
│   │   ├── hooks/
│   │   ├── agents/
│   │   └── rules/
│   └── docs/
├── install-prompt/                           # optional — sibling to workspace/
│   └── template.md.tmpl                      # install prompt scaffolding (§5)
└── (CI, README, anything else at root)       # NOT shipped to analysts
```

Only files inside `workspace/` reach the analyst. Anything at the repo
root (README, CI configs, scripts for seed maintainers) stays in the seed
repo and is invisible to the analyst.

`install-prompt/` is special: not shipped to the analyst directly, but
read by the Agnes server when rendering `/home`. It's the operator's hook
to customize the install instructions per-instance.

---

## 3. Per-file admin-editor ownership

Two admin editors flip into read-only mode automatically when the seed
provides the corresponding file. The check is per-file — an operator can
have the seed own one editor and not the other.

| Admin page                | Seed file (when present, seed wins)         | Editor behavior when seed owns         |
|---------------------------|---------------------------------------------|----------------------------------------|
| `/admin/workspace-prompt` | `workspace/CLAUDE.md`                       | Read-only, shows seed content, banner names the seed file, Save/Reset/Preview hidden |
| `/admin/agent-prompt`     | `install-prompt/template.md.tmpl`           | Same shape                              |

The corresponding API endpoints (`PUT/DELETE /api/admin/workspace-prompt-template`
and `PUT/DELETE /api/admin/welcome-template`) return `409 Conflict` with
`kind: iwt_seed_owns_template` + a `hint` field naming the seed file
when the operator tries to save against a seed-owned template.

Symmetry: when the seed lacks the file, the editor stays fully editable
and the local DB override path applies (the same path that existed
before any of this).

---

## 4. Connector skill frontmatter schema

Every `workspace/.claude/skills/connector-<slug>/SKILL.md` in the seed
MUST carry YAML frontmatter with a `connector:` block. The Agnes server
parses the block to build the connector listing `agnes onboard` prints
after setup AND the `GET /api/connectors/manifest` JSON response.

```yaml
---
name: connector-<slug>                       # MUST match the directory name
description: One-line summary used by Claude Code skill discovery.
connector:
  display_name: "Vendor Name"                # REQUIRED — string, ≤200 chars
  short_summary: "What Claude can do here."  # REQUIRED — string, ≤200 chars
  estimated_minutes: 3                       # REQUIRED — int, clamped to 0..120
  vendor_url: "https://app.vendor.com/setup" # optional — http(s) only, ≤500 chars
  requires_oauth_app: false                  # optional — bool, default false
  required: false                            # optional — bool, default false; true = mandatory install
---

<SKILL.md body — the wizard prose that Claude Code executes when the
analyst asks to set this connector up (e.g. "set up <Vendor>")>
```

**Validation rules** (enforced by `src/connectors_manifest.py`):

- `display_name` and `short_summary` strip HTML/JS (defense-in-depth on
  top of Jinja autoescape at render time), max length 200 chars.
- `estimated_minutes` is clamped to `[0, 120]`. Negative or absurdly
  large values are typo-protection — clamped, not rejected.
- `vendor_url` MUST start with `http://` or `https://` (anything else,
  including `javascript:`, is silently dropped from the manifest).
- `required` is `bool()`-coerced (like `requires_oauth_app`) — a truthy
  value marks the connector as mandatory for this instance, so it leads
  the listing `agnes onboard` prints instead of sitting among the
  optional ones. A bad value never rejects the entry.
- Invalid blocks (missing required field, wrong type, parse error) skip
  the entire connector entry with an `audit_log` warning. The rest of
  the manifest still renders — one bad seed commit can't take down
  every analyst's `/home`.

Directory names: `connector-` prefix is required. Directories not
matching `connector-*` are ignored by the manifest scan, so the seed can
freely host non-connector skills under the same `.claude/skills/` tree
without polluting the connector list.

---

### 4.1. Manifest is the allowlist for `connectors:` overlay

The `connectors:` section of `instance.yaml` (the operator-side per-
tenant overlay) is filtered against the seed-derived manifest before
it reaches the analyst's `.env`. Only keys that match a slug emitted
by the manifest scan survive; anything else is ignored and logged at
WARNING in the Agnes server logs.

Two consequences for operators:

- A typo (`connector-atlasian:` instead of `connector-atlassian:`)
  silently drops the entire block instead of writing a junk slug
  into `.env`. Check the server logs after editing the overlay if a
  connector's params don't show up on the analyst side.
- Adding a key to the overlay before the matching `connector-<slug>/`
  SKILL.md ships from the seed does NOT pre-stage the params — they
  start landing the moment the manifest entry exists and not before.

The `globals:` block bypasses the allowlist (it's not slug-scoped) and
is always emitted as-is.

Value shape: params are plain `KEY: value` string pairs, passed through
to the analyst's `.claude/agnes/.env` verbatim. That includes connector
app credentials — the server-resolved GWS fallback ships
`AGNES_GWS_CLIENT_SECRET: <value>` (a Desktop-app OAuth client secret
is an app identifier, not a user credential; the seed's `.gitignore`
covers `.claude/agnes/`). The `*_ENV` convention — shipping the NAME of
a shell env var instead of the value — is legacy: the GWS fallback still
emits the pointer alongside the value for backward compatibility, and an
operator-set pointer passes through unchanged, but nothing populates
such env vars on analyst laptops, so seed skills should read values from
the params file first.

---

## 5. `install-prompt/template.md.tmpl` placeholders

The built-in prompt is **thin** (2026-08-19): install the CLI, run
`agnes onboard`, restart Claude Code, confirm. Orchestration the prompt
used to spell out — workspace triage, catalog smoke, git/claude
preflight, marketplace bootstrap, diagnose, connector setup — happens
inside `agnes onboard`, which reports its own outcome. A seed template
that overrides the prompt should stay in the same shape; the design
rationale is in
`docs/superpowers/specs/2026-08-19-thin-install-prompt-design.md`.

What a forked template can reference — and this is narrower than the
built-in renderer's internal substitutions, because a git-bound or
DB-override prompt is rendered by the sandboxed Jinja path
(`app/web/router.py::setup_page`), not by
`app/web/setup_instructions.py::resolve_lines`:

| Reference                  | Replaced by                                          |
|----------------------------|------------------------------------------------------|
| `{server_url}`             | Browser-side at click time (JS clipboard renderer) and in the read-only preview |
| `{{ instance.name }}`      | Jinja render context (`src/welcome_template.py::build_context`) |
| `{{ instance.subtitle }}`, `{{ server.url }}`, `{{ server.hostname }}`, `{{ user.* }}`, `{{ today }}` | Same Jinja context |

Any OTHER single-brace `{name}` in a forked template renders literally —
deliberately (a typo must surface as visible text, not a 500 on `/home`).
That includes the names the built-in renderer substitutes only into its own
generated lines (`{wheel_filename}`, `{server_host}`, `{workspace_dir}`,
`{instance_brand}`) and the block placeholders the fat prompt used
(`{tls_trust_block}`, `{install_cli_block}` — never wired for forks) —
the bundled template inlines the CLI-install step instead. Retired with
the thin prompt: `{marketplace_block}`, `{connector_tiles}`,
`{ca_bundle_finale_bullet}`. `tests/test_bundled_seed_install_prompt.py`
enforces all of this on the bundled reference template.

An instance serving a self-signed / private-CA certificate should prefer
the shipped default prompt (it auto-emits the cross-platform TLS trust
block); a forked template has no way to inject that block today.

`{wheel_filename}` no longer appears in the built-in body either — step 1
downloads through the unversioned `/cli/download` endpoint, which always
serves whichever wheel is current at fetch time instead of the filename
captured when the prompt was rendered. The substitution is still applied
to every line, so a template or operator preamble that references the
filename keeps resolving.

**`{token}` is NOT a placeholder.** The analyst's access token is
deliberately never embedded in the install-prompt body — it is written to
`~/.agnes/token` out-of-band, by an earlier step of the web onboarding
flow (the "launch Claude" step that saves the token before the prompt is
generated — implemented by `/home`'s step 4: `home_not_onboarded.html` +
the shared `_claude_setup_cta.jinja` partial), so the raw value never
has to appear in the prompt text or a pasted chat transcript. A seed's
`template.md.tmpl` MUST NOT reference a `{token}` placeholder or inline a
literal token/JWT example; the prompt instead carries a guard
(`test -s ~/.agnes/token`) and `agnes onboard` reads the file itself.

A missing placeholder is rendered literally (no error). This is
deliberate — a typo in the template surfaces as visible text in the
generated install prompt rather than a 500 on `/home`.

---

## 6. Connectors are post-install, not prompt content

The install prompt carries **no** connector tiles. `agnes onboard`
finishes by listing the connectors this instance offers, and the user
sets one up afterwards by asking for it. The wizard body is fetched on
demand by `GET /api/connectors/{slug}/prompt` and never inlined
anywhere — so the install prompt stays a fixed size no matter how many
connectors an operator seeds, and a connector nobody asks for costs
nothing.

What this means for a seed author:

- The `connector-<slug>/SKILL.md` frontmatter contract (§4) is
  unchanged, including `required: true` — it is the manifest the
  post-install listing and the wizard endpoint read.
- Ordering is still alphabetical by `display_name` (case-insensitive),
  and `{instance_brand}` is substituted when the endpoint renders the
  body, as before.
- Nothing in `install-prompt/template.md.tmpl` needs to enumerate
  connectors; a template that does is duplicating a list that goes stale
  the moment the manifest changes.

---

## 7. Sync flow

```
operator → push to seed repo's main branch
operator → click "Sync now" in /admin/server-config
   ↓
Agnes server: clone --depth=1 (or fetch+reset on subsequent syncs)
   ↓
Validate the cloned tree (validate_template_tree)
   ↓
Drop the connector-manifest cache (invalidate_cache)
   ↓
Compute render dry-run (parse template, scan manifest, full render)
   ↓
POST /api/admin/initial-workspace/sync response includes render_dry_run.ok
   ↓
operator: red banner in admin UI if render_dry_run.ok = false
```

The dry-run guarantees a broken seed commit (template parse failure,
missing connector body, frontmatter regression) surfaces to the
operator before any analyst hits `/home`. Severity is split by the
`required` flag: a missing SKILL.md body is an **error** (blocks the
"seed is good" claim) for a `required: true` connector and a
**warning** for an optional one — it surfaces to the analyst only when
they ask for that connector, and the install prompt itself no longer
depends on the manifest at all.

---

## 8. CI lint recommendations (for the seed repo)

A minimal lint workflow you can copy into your seed's `.github/workflows/`:

```yaml
name: Validate seed
on: [push, pull_request]
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - name: Install pyyaml
        run: pip install pyyaml
      - name: Validate connector frontmatter
        run: |
          python3 - <<'EOF'
          import sys, yaml, pathlib, re
          ok = True
          for skill in pathlib.Path('workspace/.claude/skills').glob('connector-*/SKILL.md'):
              text = skill.read_text(encoding='utf-8').lstrip()
              if not text.startswith('---'):
                  print(f'::error::{skill}: missing frontmatter'); ok = False; continue
              body = text[3:]
              m = re.search(r'^---\s*$', body, re.MULTILINE)
              if not m:
                  print(f'::error::{skill}: unterminated frontmatter'); ok = False; continue
              try:
                  meta = yaml.safe_load(body[:m.start()])
              except Exception as e:
                  print(f'::error::{skill}: YAML parse error: {e}'); ok = False; continue
              if not isinstance(meta, dict) or not isinstance(meta.get('connector'), dict):
                  print(f'::error::{skill}: missing `connector:` block'); ok = False; continue
              c = meta['connector']
              for field in ('display_name', 'short_summary', 'estimated_minutes'):
                  if field not in c:
                      print(f'::error::{skill}: connector.{field} missing'); ok = False
          sys.exit(0 if ok else 1)
          EOF
```

This catches the same class of errors the server-side validator would,
but before a sync — operator never lands a broken commit at all.

---

## 9. Versioning (`schema_version`)

The `connector:` frontmatter block currently has no `schema_version`
field — the schema documented above is the current one.

Version history of the API response `schema_version`:

- **v2 (current)** — adds optional `connector.required` (additive; a
  seed using it still renders on older binaries, minus the mandatory
  step).
- **v1** — the initial schema.

Future schema changes will:

- Bump the API response `schema_version` on
  `GET /api/connectors/manifest` and `GET /api/connectors/params`.
- Add a `schema_version` key to the `connector:` block ONLY when a
  breaking change requires per-entry signaling.
- Document the bump in `CHANGELOG.md` with a **BREAKING** marker if the
  old shape stops parsing.

Forward-compatibility rule: unknown fields under `connector:` are
ignored, NOT rejected. A seed authored against a future schema renders
on an older Agnes binary (without the new fields' effect) instead of
breaking the manifest.

---

## 10. OSS reference seed

The OSS reference seed lives at
[`github.com/keboola/agnes-infra-template`](https://github.com/keboola/agnes-infra-template).
A snapshot of its `workspace/` + `install-prompt/` sub-trees ships
inside the Agnes wheel at `src/_bundled_seed/` (see
`scripts/sync_bundled_seed.sh` for how to refresh it). The template
repo doubles as the Terraform skeleton — operators get the deploy
stack and the seed content from the same fork.

**Suggested fork-and-customize workflow:**

```bash
# 1. Fork the OSS template (also gives you the Terraform skeleton)
gh repo create <your-org>/agnes-infra --template keboola/agnes-infra-template --private
gh repo clone <your-org>/agnes-infra

# 2. Customize workspace/ contents — rename skills, add company-specific
#    CLAUDE.md context, etc.
cd agnes-infra
vim workspace/CLAUDE.md
vim workspace/.claude/skills/connector-asana/SKILL.md

# 3. Push to your fork

# 4. Register the fork URL in your Agnes instance via /admin/server-config
#    (the Initial Workspace Template URL points at your fork's HTTPS clone URL)
```

If you also want your fork's content bundled into your custom Agnes
build (so fresh installs render your fork instead of the OSS default),
fork the Agnes repo too and run `scripts/sync_bundled_seed.sh
<branch-name> <your-fork-url>`; the bundle becomes part of your wheel.

---

## 11. Vendor-agnostic naming for forks

Agnes is vendor-neutral OSS — the upstream repo, the OSS seed, and the
bundled snapshot deliberately avoid customer-specific names. Forks
should follow the same rule INSIDE the seed:

- **Don't hardcode brand names in skill slugs.** Use `{instance_brand}`
  in skill bodies; the server substitutes it at render time. Slugs
  should stay generic (`connector-asana`, not `connector-acme-asana`).
- **Skill display names can be customized.** `display_name: "Acme Asana"`
  is fine — that's a UI string in the connector listing, not a contract.
- **Don't reference internal hostnames in seed prose.** Use placeholders
  (`<your-host>`, `example.com`); the Agnes server's substitution covers
  `{server_url}` etc.
- **Don't ship customer-specific config in seed `instance.yaml`-like
  files.** Per-tenant runtime values flow through `connectors:` in the
  Agnes server's `instance.yaml`, NOT through the seed.

The bundled snapshot ships under the OSS reference seed's URL in
`.source_ref` — your CI guard at `.github/workflows/check-bundled-seed.yml`
verifies the bundle matches that source. Forks change `.source_ref` to
point at the fork's URL; the rest of the contract is unchanged.
