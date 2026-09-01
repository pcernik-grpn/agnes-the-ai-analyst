# Attributing broker-observed LLM usage to chat turns

Status: design proposal (not yet implemented).

## Problem

Per-turn chat token capture (`usage_turns`, written by
`ChatManager._record_turn_usage` at the `assistant_message` persist seam)
works only when the provider puts usage on the frame. The engine provider
(`app/chat/kai_engine_provider.py`) translates the engine's SSE stream into
runner frames, and that stream carries no token usage — a documented
limitation ("the engine does not surface token usage on its stream"). The
manager deliberately records nothing for a no-usage frame
(`tests/test_chat_usage_turns.py::test_frame_without_usage_records_nothing`):
storing zeros would assert a measurement nobody made.

The result, verified live on an engine-provider instance: a completed
multi-tool chat turn leaves **zero** rows in `usage_turns`, `chat_messages`
token columns stay `NULL`, `/me/activity` shows 0 tokens, and — because the
same frame fields feed them — `chat.daily_anthropic_spend_usd` and
`chat.max_session_tokens` never meter engine sessions either. Engine-provider
instances are exactly where per-turn chat analytics matter most, and they are
blind.

Yet the ground truth transits our own process. Every engine LLM byte rides a
per-turn `llm`-scoped broker ticket (minted at `POST /api/kai/tickets`,
bound to the chat-session id — Agnes owns that id, the engine stores the same
UUID) through `POST /api/broker/anthropic*`. The broker already parses
provider-reported usage there (`parse_usage` in
`app/api/broker_agent_policy.py`) to feed the `llm_usage` agent-budget ledger
— which even carries `session_id`. What is missing is not observation; it is
**attribution to the session's turns**, plus two gaps in the current
recording:

- Usage collection is gated on `agent_row is not None`. Web sessions always
  resolve a bound agent (`_resolve_agent_id` falls back to the caller's
  default agent), but agent-less sessions — Slack without a channel→agent
  binding, legacy rows — collect nothing; on the SSE path the bytes are not
  even mirrored for parsing.
- `llm_usage` rows are per LLM call with only a timestamp — no turn
  boundary — and they are batch-flushed (`UsageAccumulator`), possibly from a
  different process than the one holding the live chat session, so they
  cannot be read back reliably at frame-persist time.

## Options considered

**A. Aggregate `llm_usage` rows into a turn at persist time.** Rejected:
rows are batch-flushed on their own schedule (and on process shutdown), the
broker replica that recorded them need not be the gateway holding the live
session, recording is agent-gated, and a time-window join is a fuzzy turn
boundary. Every one of those is a source of silently wrong numbers.

**B. Engine relays usage on its stream.** The correct long-term contract —
the engine's own SDK sees per-call usage and could sum it onto its finish
event; the provider would map it onto the frame fields the manager already
honors. But the engine is a separate codebase and stream protocol; this repo
cannot ship that change, and every other engine integrated tomorrow would
have to repeat it. Kept as a compatible future enhancement: the design below
gives frame-carried usage precedence, so an engine that starts reporting
usage simply takes over seamlessly.

**C. Broker-fed per-session turn counters, drained at the persist seam
(chosen).** The broker increments per-session counters in the coordination
backend on every completion it forwards; the manager drains them exactly
once per turn at the `assistant_message` persist seam and hydrates the frame
when the frame itself carries no usage. Engine-agnostic (any provider whose
LLM calls transit the broker is covered, including future ones), works
across role-split processes (the coordination backend is the existing
cross-replica seam — daily token budgets already live there), and touches no
schema.

## Design

### 1. `app/chat/turn_usage.py` — the counter seam

One small module, imported by both the broker and the manager:

- `add_turn_usage(session_id, usage)` — increment four coordination counters
  (`input`, `output`, `cache_read`, `cache_creation`; key namespace
  `chat:turnusage:{session_id}:{kind}`) via `coordination().incr`, and
  `kv_set` the completion's model (`...:model`, last writer wins). TTL well
  above any single turn's duration (24 h) — the drain each turn is what
  keeps windows short, the TTL only garbage-collects sessions that never
  come back. Never raises: `CoordinationUnavailable` is logged and
  swallowed, same posture as the broker's other recording.
- `drain_turn_usage(session_id)` — `kv_delete` each key (Redis `GETDEL`:
  atomic read-and-reset; the in-memory backend's pop has the same
  semantics) and return the totals + model, or `None` when all counters are
  absent. Destructive drain means no watermark state anywhere — a process
  restart or gateway takeover cannot double-count.

### 2. Broker: record for every session-bound completion

In the `/api/broker/anthropic*` forward path (both recording sites — the SSE
`finally` mirror and the buffered non-stream path):

- Widen the collect gate from `agent_row is not None` to
  `agent_row is not None or row.get("session_id")` (still `is_completion`
  and 2xx only), so agent-less sessions' bytes are mirrored and parsed too.
- After `parse_usage`, additionally call
  `add_turn_usage(row["session_id"], usage)` whenever the ticket carries a
  session id. The existing agent-gated `usage_accumulator.add` (the
  `llm_usage` budget ledger) is unchanged.

This covers all upstream modes (static key, WIF, dispatcher, vertex) because
both sites sit downstream of the mode fork.

### 3. Manager: drain once per turn, hydrate the frame

In `_handle_frame`, at `assistant_message`, **before** `append_message`:

- Drain the session's counters.
- If the frame carries no usage (all four token fields `None`) and the drain
  returned totals: stamp `tokens_in`/`tokens_out`/`cache_read_tokens`/
  `cache_creation_tokens`/`model` onto the frame.
- If the frame already carries usage: discard the drained values. This is
  the double-count guard — native-sandbox LLM calls transit the same broker
  route with session tickets, so their counters accumulate too; the frame's
  self-reported numbers win, and the discard stops residue from leaking into
  the next turn's drain.

Everything downstream is existing code, untouched: `append_message` persists
the tokens into `chat_messages`, `_record_daily_tokens` feeds the daily
spend counters, `_record_turn_usage` writes the `usage_turns` row (model,
four token kinds, surface, `chat-<id>.jsonl` session key). One hydration
point, four consumers.

Failure posture: `CoordinationUnavailable` at drain time → skip hydration;
the turn persists exactly as today (unmeasured), never blocked or failed.

### 4. Deliberate behavioral consequences

- **Engine sessions become metered.** `chat.daily_anthropic_spend_usd` and
  `chat.max_session_tokens` (which sums `chat_messages` tokens) start
  applying to engine sessions — closing the limitation stated in
  `kai_engine_provider.py` and `docs/cloud-chat.md`; both texts are updated
  in the same change. Operators who relied on engine sessions being exempt
  from the caps will see caps fire; that exemption was an accident of
  blindness, not a promise.
- **Agent-less sessions get turn usage.** Slack sessions without a
  channel→agent binding stop being a blind spot for `usage_turns` (their
  per-call `llm_usage` ledger remains agent-only — budgets are an agent
  concept).

### 5. Precision, stated rather than implied

- Counters are **conserved, not turn-perfect**: a completion finishing
  exactly at the drain boundary attributes to the adjacent turn; totals per
  session are always right. In practice the broker records before the engine
  can even see its stream end, so the window is theoretical.
- Counters left undrained when a session ends (a straggler call after the
  final answer) expire with the TTL — an accepted, bounded loss.
- A multi-model turn records the **last** completion's model (typical engine
  turns run one main model; utility calls are marginal). Cost-at-read then
  prices the whole turn at that model.
- Broker-observed usage is provider-reported ground truth — the same
  `usage` block a frame would have carried.

## Non-goals

- Changing the `llm_usage` ledger, budget enforcement, or its agent gate.
- Ticket-id-level turn correlation or a provenance column on `usage_turns`
  (no migration in this change).
- Backfilling historical blind turns.
- Engine stream-protocol changes (option B stays open and composes).

## Testing

- **Seam unit tests**: `add_turn_usage`/`drain_turn_usage` round-trip on the
  in-memory backend; drain-when-empty returns `None`; unavailability never
  raises.
- **Manager** (extend `tests/test_chat_usage_turns.py`): a no-usage frame
  with seeded counters writes one hydrated `usage_turns` row (surface,
  model, all four kinds) and persists tokens on the message; a frame **with**
  usage records the frame's numbers and leaves the counters drained; a
  no-usage frame with no counters still records nothing (the existing
  `test_frame_without_usage_records_nothing` contract narrows to "nothing
  brokered either"); coordination down → today's behavior.
- **Broker** (`tests/test_broker_routes.py`): a session ticket with no bound
  agent accumulates counters on both the SSE and JSON paths; no session id →
  no counters; `llm_usage` recording unchanged.
- **Live acceptance** on an engine-provider instance: one multi-tool web
  turn → one `usage_turns` row (`surface='web'`), non-zero tokens in
  `/me/activity`, and the admin chat-cost view prices it.

## Rollout

No migration, no config knob, not breaking. CHANGELOG: Added (per-turn usage
for engine-provider chat sessions, attributed at the broker) + Changed
(engine sessions are now metered by the daily-spend and per-session token
caps).
