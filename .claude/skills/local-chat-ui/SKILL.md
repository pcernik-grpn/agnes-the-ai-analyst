---
name: Local Agnes Chat UI testing
description: How to run the Agnes FastAPI server locally so the chat UI and onboarding rail render without real auth or LLM credentials.
---

# Local Agnes Chat UI testing

Use this when you need to test `/chat`, the onboarding rail/panel, or any UI that depends on `can_chat`.

## Start the server

```bash
cd <install-dir>   # your local checkout of this repository
LOCAL_DEV_MODE=1 TESTING=1 AGNES_CHAT_ENABLED=true .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 9000
```

- `LOCAL_DEV_MODE=1` auto-authenticates every request as `dev@localhost` (admin).
- `TESTING=1` bypasses the `JWT_SECRET_KEY` and `ANTHROPIC_API_KEY` startup checks (and the chat provider-backing boot gates).
- `AGNES_CHAT_ENABLED=true` enables chat; without it the rail panel is never rendered.
- No `instance.yaml` is required; the app falls back to built-in defaults.

This server has **no working turn engine**: with no LLM credentials and no
provider backing a real message cannot be answered, so it is only good for
rendering, layout and onboarding work.

## Testing an actual conversation (tool cards, streaming, frame order)

**Do not** fake frames by injecting into the WebSocket `onmessage` handler from
the browser console. It looks like it works and it lies: nothing reaches the
server, so no message is persisted, no title is generated, and the reload path
renders an empty session — three "bugs" that are really artifacts of the
harness. It also cannot catch anything in the provider, the manager or the
frame envelope, which is where real defects live.

Run the **kai-agent stub engine** instead — a scripted stand-in that speaks the
engine's SSE contract over real HTTP, so a turn travels the whole production
path (provider → manager → WebSocket → renderer) with no model and no key:

```bash
KAI_HOST_JWT_SECRET=local-dev-secret KAI_STUB_HOST=127.0.0.1 .venv/bin/python -m services.kai_engine_stub
```

```bash
LOCAL_DEV_MODE=1 TESTING=1 AGNES_CHAT_ENABLED=true AGNES_CHAT_PROVIDER=kai-agent AGNES_CHAT_KAI_AGENT_URL=http://127.0.0.1:3000 KAI_HOST_JWT_SECRET=local-dev-secret .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 9000
```

Then type a keyword to pick the turn shape: `interleaved` (text → tool → text →
tool → text), `table`, `fail`, `approval`, `error`, `markdown`. Full reference,
compose profile, knobs and fidelity limits:
[`docs/kai-agent-local-dev.md`](../../../docs/kai-agent-local-dev.md).

Note `title` stays `NULL` in local dev either way — auto-title calls Haiku
directly and skips silently without `ANTHROPIC_API_KEY`. That is expected.

## Enable the onboarding rail panel

The rail row is gated by `can_chat = chat_config.enabled && has_explicit_grant(user["id"], "chat", "chat")`. In local dev the dev user belongs to the seeded `Admin` and `Everyone` groups, but there is no `chat` grant by default. Create one via the admin REST API after startup:

```bash
curl -s http://127.0.0.1:9000/api/admin/groups
# Find the `Everyone` group id, then:
curl -X POST http://127.0.0.1:9000/api/admin/grants \
  -H 'Content-Type: application/json' \
  -d '{"group_id": "<everyone-group-id>", "resource_type": "chat", "resource_id": "chat"}'
```

Without this grant the "Set up Agnes" row will not appear in the rail and `chat_onboarding.js` cannot be exercised end-to-end.

## Useful checks

- `GET http://127.0.0.1:9000/api/chat/journey` returns the current onboarding state.
- `PUT http://127.0.0.1:9000/api/chat/journey` with any subset of the journey flags updates it; all six flags set to `true` marks onboarding as complete (`6/6`), and setting all to `false` plus `onboarded: false` re-arms it.
- The local dev user is `dev@localhost`; the UI profile menu contains "Start over onboarding" once the checklist has been completed or skipped.

## Browser notes

Chrome launched with `--no-sandbox` may show an unsupported command-line banner; it does not affect the test. The page can be driven at `http://127.0.0.1:9000/chat`. The rail's "Set up Agnes" popover opens via `#rail-getstarted-toggle` and is rendered by `chat_onboarding.js`.

## Devin Secrets Needed

None for this local-only flow.
