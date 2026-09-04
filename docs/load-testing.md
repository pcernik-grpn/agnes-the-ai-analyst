# Load testing a live instance

Three scripts under [`../scripts/stress/`](../scripts/stress/) measure what a room full
of people actually experiences: provision throwaway identities, drive them through a
journey over the same WebSocket a browser uses, and capture the server-side evidence the
dashboards cannot see.

```
provision.py   N identities + a group + its grants, and the teardown that revokes them
runner.py      N concurrent virtual users through a journey, one JSONL row per phase
watch.py       live container logs + cgroup CPU throttling over SSH, and the abort
```

The journey is a YAML file you supply, so nothing here carries a question, a corpus or a
customer — point it at any instance.

## Why the client half is the point

An instance's own metrics can say the host was idle and the database was bored while
every person in the room stared at a blank screen. Time to first frame is only
measurable from the client, and it is the number that decides whether a demo goes well.

Three timings per turn, and they answer different questions — conflating them is the
single easiest way to misread a run:

| | what it measures |
|---|---|
| `ready_ms` | the manager seated your socket. **Not** the sandbox becoming usable — the spawn path returns without awaiting the runner, so this lands under a second even on a cold session and carries no information about spawn cost. |
| `first_activity_ms` | the agent's first frame of any kind (tool call, token, message). **The blank-screen wait, and the only timing containing the sandbox spawn.** Use this as the latency metric. |
| `first_token_ms` | the first token of prose. On a turn that searches before it speaks, this trails `first_activity` by however long the tool work took — so it tracks the *question's* depth as much as the system's speed. Never read it as latency. |
| `done_ms` | the whole turn. Dominated by how many tools the agent chose to call; not a load indicator. |

A run measured with `first_token_ms` will report a follow-up turn as five times slower
than a cold one and conclude the wrong thing.

## Provisioning identities

`provision.py` prefers **service accounts** — they need no login provider, they can never
hold an interactive session, they can never join Admin, and deactivating one revokes
every PAT it holds in a single call.

```bash
export AGNES_ADMIN_TOKEN="$(cat /path/to/token)"

python scripts/stress/provision.py create \
    --base-url https://<host> --count 20 --state /path/identities.json
```

The admin token must be an **interactive session JWT** (the browser's `access_token`
cookie), not a PAT: minting a durable credential is session-token-only by design. It is
read from `$AGNES_ADMIN_TOKEN` or `--admin-token-file` and is **deliberately not
accepted as a command-line value** — argv is readable by every process on the host for
as long as the call runs, and this is the one credential that can mint others.

On a build without the service-account route, `--identity-kind user` creates ordinary
password users instead. Read the trade-off before using it: these accounts *can* sign in.
The script narrows the exposure — addresses land in a `.invalid` domain (RFC 2606, no
mail, and it can never appear in an SSO allowlist), the password exists only inside the
function that mints the PAT and is never stored, and `/auth/token` calls are paced under
that endpoint's own rate limit.

The state file holds raw PATs. It is written `0600` and never echoed.

### Grants are not isolation

A new identity joins `Everyone` automatically, so it inherits whatever `Everyone` can
reach — on a populated instance that is the real corpus. The dedicated group is an
explicit, revocable membership set, **not** a boundary. Which means:

> **An active load identity is an account with live access to production data.** Teardown
> deactivating them is the security step, not housekeeping. Run it even when a run aborts.

```bash
python scripts/stress/provision.py teardown \
    --base-url https://<host> --state /path/identities.json \
    --purge-identities --purge-group

python scripts/stress/provision.py verify \
    --base-url https://<host> --identity-kind user
```

`--purge-group` deletes the group **only if this run created it**. A group adopted by
name is left alone, grants and all: reusing one across a ramp is convenient, but the id
in the state file may belong to somebody else.

`verify` re-reads the server's own listing rather than the state file, so it also catches
an identity an interrupted earlier run left behind.

## The journey

```yaml
name: example
steps:
  - {phase: dashboard,      kind: http, method: GET, path: /}
  - {phase: catalog,        kind: http, method: GET, path: /catalog}
  - {phase: session_create, kind: chat_open, surface: web}
  - {phase: turn_cold,      kind: turn, text: "a question that needs retrieval"}
  - {phase: turn_warm,      kind: turn, text: "a follow-up"}
  - {phase: detach,         kind: ws_close}
  - {phase: idle,           kind: sleep, seconds: 90}   # > idle_grace_seconds
  - {phase: reattach,       kind: chat_reopen}
  - {phase: turn_resumed,   kind: turn, text: "one more"}
  - {phase: close,          kind: ws_close}
```

Step kinds: `http`, `chat_open`, `chat_reopen`, `turn`, `ws_close`, `sleep`.

The three-turn shape exists to produce three different `first_activity_ms` numbers from
one journey — cold spawn, warm follow-up on the same socket, and a resume after the
sandbox has paused. The idle must exceed `chat.idle_grace_seconds` (default 60 s) or the
last turn measures a warm one again.

## Running a wave

One invocation is **one** concurrency step. A ramp is several invocations with a look at
the results in between; that pause is the whole value of ramping.

```bash
python scripts/stress/runner.py \
    --base-url https://<host> \
    --identities /path/identities.json \
    --journey /path/journey.yaml \
    --users 20 --stagger 30 --step-label ramp20 \
    --abort-file /path/ABORT \
    --out /path/ramp20.jsonl --verbose
```

`--stagger` spreads the cohort's starts over that many seconds. It matters more than it
looks: **the constraint a load test finds is usually the arrival rate, not the user
count.** Keep it constant across a ramp or the waves are not comparable.

Output is JSONL (one row per phase) plus `<out>.manifest.json` carrying the run's UTC
window — use that window to scope dashboard queries to the wave.

`--dry-run` validates the journey and the identity count without touching the network.

### Reading the results

- **The output file must not already exist.** The manifest summarises only the rows the
  current invocation produced, so appending to a previous run would leave the data and
  its summary describing different things, with nothing in either saying so.
- **The tokens are bound to the host they were minted for.** A state file naming a
  different instance is refused rather than presented to it (`--allow-host-mismatch`
  overrides, deliberately awkwardly).
- **A journey that reached its last step is not a success.** A failed turn is recorded
  and the journey walks on, so `outcomes[].completed` is false whenever any phase
  errored. Check `summary.errors`, never the wall clock alone.
- **`seq_gap` must be 0.** Non-zero means frames were dropped between server and client.
- **Cross-talk is checked on the frame envelope** (`id = "{chat_id}:{seq}"`), not on
  answer text, so it holds for real questions with no injected marker.
- Error names are assigned where the evidence exists: `concurrency_cap`,
  `budget_exhausted`, `chat_access_denied`, `chat_disabled`, `pool_exhausted`,
  `frame_error`, `first_token_timeout`, `turn_timeout`, `stream_broken`, `truncated`.

## Watching the server

Two things a load run needs that no dashboard provides.

**The application log.** A connection-pool exhaustion (`QueuePool limit of size N
overflow M`) is a ceiling *inside* one process: the database never looks busy and the
host never looks loaded, so the only place it is ever stated is the app's own log.

**CPU throttling, fresh.** A container against its `cpus:` cap is throttled by the kernel
long before any host metric moves — and the quota is enforced **per 100 ms period, not
per minute**, so a container can average a third of its cap for a minute while being
throttled solid for a second inside it. A metrics backend sampling every 60 s cannot see
that burst. This reads the cgroup's own `cpu.stat` once a second.

```bash
python scripts/stress/watch.py \
    --ssh "<ssh command prefix taking a remote command as its last argument>" \
    --service app --service kai-agent \
    --out-dir /path/run-dir \
    --warn-throttle 0.25 --abort-throttle 0.80 --abort-consecutive 10 \
    --abort-file /path/ABORT --duration 2700
```

`--ssh` is the command prefix (`ssh host`, `gcloud compute ssh … --command`, anything
that takes a remote command last), so the deployment's coordinates stay in your shell and
out of this repo. `--service` is repeatable — **watch every service that can hold the
evidence.** An agent-engine failure is stated in the engine's log, not the app's.

The streams reconnect on their own, resuming the log from the last timestamp received
rather than from the live tail — the gap during a disconnect is exactly where a
saturation event hides. A tunnel that drops mid-wave otherwise takes the rest of that
wave's record with it, and it will drop. If a stream cannot be reopened at all, the
watcher says so loudly and exits non-zero: a run whose capture died must not be
mistaken for a clean one.

### The abort

The abort is a file: `watch.py` writes it, `runner.py --abort-file` sees it between steps
and stops starting new work. Never inside a turn — cutting a turn mid-stream would record
a truncation the server never caused; the point is to stop *adding* load. A stale abort
file refuses to start a run.

Set the throttle threshold from a measurement, not a guess: sample the idle baseline and
a single-user run first, then put the abort several times above the longest sustained
run a single user produces.

Deliberately **two** levels, because a ramp exists to *find* saturation: `--warn-throttle`
prints and never stops; `--abort-throttle` stops. A `QueuePool` line aborts by default —
once seen, more load only repeats it.

## Cost and blast radius

Every turn is a real LLM call against the instance's configured provider, billed
wherever that provider bills. A three-turn journey ran roughly $1.20 in one measured
deployment; a 40-user wave is 120 turns. Estimate before a ramp, and check
`chat.daily_anthropic_spend_usd` and `chat.rate_messages_per_hour` on the target — a run
that trips a limit measures the limit, not the system.

Announce the window. This is real load on a real instance, and other people's sessions
are in the measurement whether you account for them or not.
