---
name: agnes-data-apps-extras
description: Agnes-specific cadence for AI-authored hosted data apps — scaffold-first, draft-branch discipline, in-chat preview, and promote flow. Use whenever a chat user asks you to build, edit, or ship a dashboard/web app; loads alongside the general dataapp-development skill, never instead of it.
---

# Agnes data-apps extras

This skill is the **Agnes-specific overlay** on top of the general
`dataapp-development` skill (vendored from the upstream `keboola/ai-kit`
marketplace). Load both — `dataapp-development` owns the `keboola-config/`
runtime contract, port wiring (nginx `:8888` in front of the app on `:3000`),
storage-access patterns, and troubleshooting; this skill never re-derives any
of that. This skill only adds: which scaffold to start from, which git branch
to touch, how to drive the in-chat preview, and how Agnes deploys/promotes
differ from the other harnesses `dataapp-development` already knows about.

If you haven't loaded `dataapp-development` yet, load it now. If you're
unsure which deployment path applies, see `references/path-d.md` — Agnes is
detected by the presence of the `data_app_*` MCP tools.

## 0. Create the app, then its draft, before anything is deployed

For a NEW app the order is fixed, and every step is a precondition of the
next — watched live, skipping either of the first two stops the run:

1. `data_app_create(slug, name, description)` — the registry row and its
   empty repo. `data_app_create_draft` against a slug that was never created
   returns `404 data_app_not_found` (a run used to stall here, retrying the
   draft call and getting the same 404).
2. Seed the repo: clone through the relay (§0b), copy the scaffold (§1),
   commit, push to `main`.
3. `data_app_create_draft(slug)` — a draft is a sibling row pinned to a
   branch of the same repo. **Every dev deploy needs one, including the very
   first**: `data_app_deploy(<slug>, mode="dev")` on the prod row fails with
   `400 dev_requires_draft` (watched live: the agent deployed the prod slug
   in dev mode, got the 400, and only then created the draft).
4. `data_app_deploy(<draft_slug>, mode="dev")`, then the preview cadence
   in §3.

Never deploy the prod row (`data_app_deploy(slug)` with no `mode`) before the
user has picked "Publish" — that is the promote flow
(`references/promote-flow.md`), not the first deploy.

For an app that already exists, skip step 1; if it has no open draft, start
at step 3.

## 0b. Cloning the repo: use the relay, not the credential URL

From a chat sandbox, clone and push through the **relay**:

    git clone "$AGNES_SERVER_BASE/data-apps.git/<slug>" app-repo

where `$AGNES_SERVER_BASE` is the loopback origin the `agnes` CLI already
talks to (`http://127.0.0.1:<port>`) — the host part of `AGNES_SERVER`,
without its `/agnes-api` path. No credential goes in that URL and none is
needed: the relay attaches one server-side, which is the whole reason it
exists.

The relay's **port changes every time the runner starts**, so a URL recorded
in `.git/config` goes stale the moment a paused sandbox resumes — the clone
worked, and the next `git push` fails to connect. Re-point the remote from
the environment before pushing, rather than trusting what the clone wrote:

    git -C app-repo remote set-url origin "$AGNES_SERVER_BASE/data-apps.git/<slug>"

`$AGNES_SERVER_BASE` is re-exported by every runner start, so it is always
the live one.

Do **not** use the URL from `data_app_git_credential(slug)` here. That one
carries an embedded token and points at the deployment's public host, which
a sandbox cannot reach — its egress allowlist admits loopback, Anthropic and
GitHub, nothing else. It is for an analyst laptop or an MCP client, not for
you. Reaching for it inside the sandbox is what a run does right before it
stalls, having tried the hostname, then the IP, then the sandbox bypass.

## 1. Scaffold-first, custom-code-second

Never start from a blank repo. `cp -R` the baked scaffold at
**`/work/scaffolds/nodejs-dashboard/`** — an absolute path, at the session
workspace root. It is **not** inside this skill's own directory: watched
live, "sibling of this skill's session workspace" was read as a path
relative to the skill, and three `ls`/`cp` attempts against
`.claude/skills/agnes-data-apps-extras/scaffolds/` all failed with "No such
file or directory" before the run stalled. Copy it into the app's managed
repo **before** writing a single line of real code,
then immediately call `data_app_deploy(draft_slug, mode="dev")`. This boots
the container and warms `npm install` while you write the real
`src/App.tsx` / `server/index.ts` — HMR picks up your edits from there. Do
not wait for "real" code before the first deploy; a cold first deploy racing
`npm install` is exactly the failure mode this cadence avoids.

## 2. Draft-branch discipline

Every app-code change happens on the draft's pinned branch — **never** push
to `main`. `main` only gains code at promote time (see
`references/promote-flow.md`). If you need to resume work on an existing
draft, mint a fresh credential with `data_app_git_credential(slug)` rather
than reusing a stale one.

## 3. Preview cadence

Call `agnes_data_app_preview(slug)` with no `url` as soon as the dev deploy
starts — this opens a placeholder pane immediately so the user isn't staring
at nothing. Hold the **live** call (`agnes_data_app_preview(slug,
url="/apps/<draft_slug>/")`) until the real dashboard is pushed *and* the dev
deploy is healthy: poll `data_app_get(draft_slug)` in short steps (≤5s), not
one long sleep. Once the live preview is up, ask the user via
`AskUserQuestion`: **"Publish" or "Make changes"** — and stop there. Never
promote on your own initiative; promotion is always an explicit user choice.
If the user asks for changes, keep iterating on the draft branch and re-open
the live preview when ready. If they publish, follow
`references/promote-flow.md`.

Use `agnes_data_app_refresh(slug)` after a dependency change or a `mode=dev`
redeploy (HMR does not pick those up); ordinary `src/**`/`server/**` edits
don't need it. Call `agnes_data_app_close(slug)` before tearing down a draft
(`data_app_delete_draft`) so the pane never points at a deleted app.

## 4. Visual-quality bar, chat voice, jargon ban

- Real React + Vite + Tailwind. Charting libraries come from npm
  (`package.json`), never a CDN `<script>` tag.
- Chat messages are 1–2 short sentences. No walls of text.
- Never leak implementation jargon to the user: say "setting up your app",
  not "pushing the scaffold to the draft branch"; say "app is ready to look
  at", not "dev deploy reports healthy". Git, HMR, supervisord, and deploy
  mechanics are your concern, not the user's.

## 5. Debugging

The only observable signal is `data_app_logs(slug)`. Use it to explain
failures in terms of row counts, redacted SQL, and which code path ran —
never surface cell values or PII from the logs back to the user.

## 6. Context persistence

The managed repo's root `CLAUDE.md` carries one maintained section:
`# App context (maintained by Agnes)` (Purpose / Data sources / Key
decisions / Iterate safely — see the scaffold's `CLAUDE.md.tmpl` for the
skeleton). Write/update it at promote time; read it back at the start of any
fresh conversation that edits an existing app, so you inherit prior
decisions instead of re-deriving them.

## 7. Reading Agnes data

The scaffold's `server/agnesQuery.ts` wraps the app's injected
`AGNES_TOKEN` / `AGNES_URL` env vars against the Agnes REST API
(`runQuery(sql)` over `/api/query`, plus catalog/table lookups). See
`references/agnes-query.md` for the exact helper shape. This is the *only*
way the app reads Agnes data — never hardcode credentials, never bypass the
owner-scoped token.

Where a figure corresponds to a defined business metric, read the metric's
definition and run *its* SQL instead of writing your own — see the
"Metrics before hand-written SQL" section of that reference.

## 8. What the app must never expose

Everything the app serves is reachable by everyone holding a grant on it — with
a browser, and with `agnes app fetch`. Two mistakes turn that into a leak.
`POST /api/data-apps/{slug}/deploy` now runs a best-effort static scan for
both, plus a couple of related ones, before promoting a commit to `agnes-live`
(`src/data_apps/deploy_check.py`, #1946) — `agnes app deploy` prints any
findings, and the server can be configured (`data_apps.deploy_checks`) to
refuse a deploy outright instead of just warning. It is a signal, not a
guarantee: write the app so none of these ever fire.

**Never serve a static directory at or above the project root** (`DA001`
Node `express.static`/`serveStatic`/fastify-static, `DA002` Python
`StaticFiles`/`send_from_directory`/`app.static_folder`, `DA003` nginx
`root`/`alias`). The shipped scaffold does not: its nginx config is a single
`location /` proxying to the app process, with no `root` and no `alias`, so
nothing on disk is served unless you add it. An `express.static(<project
root>)` makes every committed file downloadable — fixtures, dumps, a `.env`
someone checked in by mistake. Serve a named subdirectory of built assets,
never the root — the scaffold's own `express.static(distDir)` does exactly
that.

**Never return `process.env`, or any part of it, in a response** (`DA004`).
This is the sharper one. The runtime is handed the app owner's real
`AGNES_TOKEN` as an environment variable, so a debug endpoint, a verbose
error page, or an exception rendered with the environment attached hands out
a live Agnes credential to anyone who can open the app. Read the variables
you need into local constants and use them to build outgoing requests; never
put the environment itself, or an error object that closes over it, into a
response body. The same scan separately flags the token itself being echoed
back on a response line (`DA005`) and debug mode left on (`DA006`,
informational only).

The scaffold's error handling is a worked example: it returns the upstream
response text on failure, never the request headers it sent.

## References

- `references/path-d.md` — how Agnes shows up as a deployment path in the
  general skill's detection logic (interim overlay pending upstream merge).
- `references/agnes-query.md` — the `agnesQuery.ts` helper contract.
- `references/promote-flow.md` — the exact draft→prod git sequence.
