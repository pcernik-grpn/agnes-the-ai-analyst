# Running `chat.provider: kai-agent` locally

The kai-agent turn engine lives outside this repository, so for a long time
`chat.provider: kai-agent` was the one provider nobody could exercise on a
laptop. The unit tests fake the engine with an in-process httpx transport
(`tests/test_kai_engine_provider.py`), which pins the SSE→frame translation but
never touches the wire, the manager, the WebSocket or the renderer. Everything
downstream of the provider had to be checked by hand, and a whole class of
defect hid in that gap — issue #1504 (tool calls rendered after the answer
instead of at their position) was one of them, plus a failed tool that rendered
with a success tick because the error verdict was being dropped on the frame.

`services/kai_engine_stub` closes the gap: a development stand-in that speaks
the engine's HTTP surface with scripted turns. No model, no Anthropic key, no
database. It is a **protocol fixture**, not a simulator — a turn's shape is
chosen by keyword from your message, so "reproduce the interleaved-tool-call
bug" is a typed sentence rather than a hand-rolled JavaScript injection.

> **Never enable this in a deployed environment.** It answers every turn with a
> canned script. It is gated behind its own compose profile so it cannot appear
> in a stack that did not name it.

## Local processes (no Docker)

Two terminals. The shared `KAI_HOST_JWT_SECRET` must match: the stub verifies
the host-minted session JWT exactly as the real engine does, so a drifted
issuer or a secret with a trailing newline fails here instead of only in an
environment you cannot debug from a laptop.

Terminal 1 — the stub engine:

```bash
KAI_HOST_JWT_SECRET=local-dev-secret KAI_STUB_HOST=127.0.0.1 .venv/bin/python -m services.kai_engine_stub
```

Terminal 2 — Agnes, pointed at it:

```bash
LOCAL_DEV_MODE=1 TESTING=1 AGNES_CHAT_ENABLED=true AGNES_CHAT_PROVIDER=kai-agent AGNES_CHAT_KAI_AGENT_URL=http://127.0.0.1:3000 KAI_HOST_JWT_SECRET=local-dev-secret .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 9000
```

Then open `http://127.0.0.1:9000/chat`. Confirm the wiring in the app's startup
log before you debug anything else:

```
chat.enabled: ChatManager started (provider=kai-agent, engine=http://127.0.0.1:3000, ...)
```

`AGNES_CHAT_KAI_AGENT_URL` exists because the default `chat.kai_agent_url`
names a compose service (`http://kai-agent:3000`) that does not resolve outside
the compose network, and because the local-dev flow deliberately runs without
an `instance.yaml`. It follows the same env > yaml > default precedence as
`AGNES_CHAT_PROVIDER`.

Both processes are also registered in `.claude/launch.json`
(`kai-engine-stub` and `agnes-dev-kai`) for anyone driving the app through the
Claude Code browser preview.

## Docker Compose

The stub answers to the hostname `kai-agent`, which is what the default
`chat.kai_agent_url` already points at, so no URL override is needed:

```bash
KAI_HOST_JWT_SECRET=local-dev-secret docker compose --profile kai-stub up
```

Set `chat.provider: kai-agent` in `config/instance.yaml`, or pass
`AGNES_CHAT_PROVIDER=kai-agent` in the app's environment.

## Scenarios

The stub picks a turn shape by matching a keyword against your message
(`SCENARIOS` in `services/kai_engine_stub/api.py`). Anything unmatched gets the
default turn, which names the others.

| Type this          | You get                                                        |
| ------------------ | -------------------------------------------------------------- |
| `interleaved`      | text → tool → text → tool → text; an MCP envelope and a 6-row table. The #1504 shape. |
| `table`            | a 400-row result, to see the preview cap and the "show all rows" route |
| `fail`             | a tool that fails with `tool-output-error` — red card, auto-opened |
| `approval`         | a `tool-approval-request`; the turn blocks until you Allow or Deny |
| `error`            | a mid-turn engine `error` event after partial text             |
| `markdown`         | a tool returning a markdown table, rendered as a real table    |

Knobs: `KAI_STUB_STEP_DELAY` (seconds between SSE records, default `0.35` so
the interleaving is visible to a human), `KAI_STUB_APPROVAL_TIMEOUT`,
`KAI_STUB_REQUIRE_AUTH=0` to accept any bearer while debugging the transport
itself.

## Automated coverage

`tests/test_kai_engine_stub.py` drives the stub through the real
`KaiEngineProvider` over a real socket (uvicorn in a thread — `ASGITransport`
buffers the response body, so a turn that blocks mid-stream on an approval
deadlocks under it). It pins the frame ORDER of an interleaved turn, the
approval round trip, the `is_error` verdict on a failed tool, and that the MCP
envelope reaches the client unflattened.

What it does not cover is the browser: the renderer is still verified by
opening the page. When you change `app/web/static/js/chat.js`, drive at least
the `interleaved` and `fail` scenarios by hand and check that text and cards
alternate in wire order, and that the failed card is red and open. Then
**reload the page** and check the transcript is unchanged — the turn's shape
is persisted as `chat_messages.parts`, so a reload that differs from the live
render is a bug in one of the two paths.

## Fidelity limits

Worth knowing before you trust a local result:

- **Scripted turns.** The stub never calls a model and never runs a tool, so it
  cannot tell you anything about prompt behaviour or tool semantics.
- **No transcript store.** The real engine keeps its own conversation history
  in Postgres and serves multi-turn context from it; the stub treats every turn
  independently. Agnes's own `chat_messages` persistence is unaffected and does
  work locally.
- **No tickets, no workspace fetch.** The stub does not call back to
  `/api/kai/tickets` or `GET /api/kai/workspace`, so the broker scope split and
  the workspace tarball path are not exercised — `tests/test_kai_host.py`
  covers those from the host side.
- **Auto-title still needs a real key.** `chat_sessions.title` stays `NULL`
  locally because auto-title asks Haiku directly and skips silently (at debug
  level) when neither `ANTHROPIC_API_KEY` nor a WIF configuration is present.
  A null title in local dev is expected, not a bug.
