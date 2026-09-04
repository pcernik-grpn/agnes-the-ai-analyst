"""Drive N concurrent virtual users through a journey and measure it.

One asyncio task per virtual user, each holding its own PAT and its own
chat session. The journey — which pages to fetch, which questions to ask,
how long to idle — is data (a YAML file), so this module carries no
domain, customer or corpus content and can measure any instance.

The client half is the point: the server's own metrics cannot see time to
first token, and that is the only number that says whether a room full of
people is having a good time. Everything here is measured from the socket
a browser would have used — the web-chat WebSocket, not ``/api/v1`` — so
the numbers describe the path real users take, including its sender-limit
and reconnect behavior.

One invocation runs ONE ramp step. A ramp is several invocations with a
look at the dashboard in between; that pause is the whole value of ramping
and is deliberately not automated away.

Output is JSONL, one row per measured phase, plus a run manifest carrying
the UTC window (for scoping dashboard queries to this step) and a summary.

Usage::

    python scripts/stress/runner.py \\
        --base-url https://host \\
        --identities /path/identities.json \\
        --journey /path/journey.yaml \\
        --users 1 --out /path/run.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
import yaml

try:
    from websockets.asyncio.client import connect as _ws_connect

    ws_connect: Any = _ws_connect
except ImportError:  # pragma: no cover — websockets rides in via uvicorn[standard]
    ws_connect = None


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------

# Sized off an observed single-user baseline (first token p50 ~16 s, whole
# turn ~20 s) with room for the degradation the run exists to find: a timeout
# that fires before the server gives up turns a slow answer into a missing
# data point, which is the one outcome that teaches nothing.
DEFAULT_TIMEOUTS = {
    "http": 60.0,
    "ws_open": 30.0,
    "ready": 180.0,
    "first_token": 240.0,
    "turn_done": 420.0,
}


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def classify_http(status: int, body: Any) -> Optional[str]:
    """Name the failure behind an HTTP status, or None when it succeeded.

    The names are the vocabulary the analysis groups on, so they are
    assigned here at the point the evidence exists rather than inferred
    later from a status code that has lost its body.
    """
    if 200 <= status < 400:
        return None
    text = json.dumps(body) if not isinstance(body, str) else body
    detail = text.lower()
    if status == 429:
        if "concurrency_cap" in detail:
            return "concurrency_cap"
        if "budget" in detail:
            return "budget_exhausted"
        return "rate_limited"
    if status == 403:
        return "chat_access_denied"
    if status == 401:
        return "unauthorized"
    if status == 503:
        return "chat_disabled"
    if status >= 500:
        # H1's fingerprint. The app returns a generic 500 body, so this only
        # fires when the pool error leaks into it; the authoritative check
        # is `QueuePool limit` in the app log for the same x-request-id.
        if "queuepool" in detail:
            return "pool_exhausted"
        # Sender limits (100 msg/h, daily spend) surface on /api/v1 as an
        # opaque 500 rather than a 429 — see the side findings in the plan.
        return "server_error_maybe_sender_limit"
    return f"http_{status}"


# ---------------------------------------------------------------------------
# Journey model
# ---------------------------------------------------------------------------


@dataclass
class Step:
    phase: str
    kind: str
    raw: dict

    @property
    def path(self) -> str:
        return str(self.raw.get("path", "/"))

    @property
    def text(self) -> str:
        return str(self.raw.get("text", ""))

    @property
    def seconds(self) -> float:
        return float(self.raw.get("seconds", 0))


@dataclass
class Journey:
    name: str
    steps: list[Step]

    @staticmethod
    def load(path: Path) -> "Journey":
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict) or "steps" not in raw:
            raise SystemExit(f"{path}: expected a mapping with a 'steps' list")
        steps: list[Step] = []
        for i, item in enumerate(raw["steps"]):
            if not isinstance(item, dict) or "kind" not in item:
                raise SystemExit(f"{path}: step {i} needs a 'kind'")
            kind = str(item["kind"])
            if kind not in _STEP_KINDS:
                raise SystemExit(f"{path}: step {i} has unknown kind {kind!r}; known: {sorted(_STEP_KINDS)}")
            if kind == "http":
                # httpx applies the client's Authorization header to an
                # ABSOLUTE request URL too, so a journey naming another host
                # would hand it the load identity's PAT — the identities-file
                # host check cannot see this, because the journey is a
                # separate input.
                step_path = str(item.get("path", "/"))
                if "://" in step_path or step_path.startswith("//"):
                    raise SystemExit(
                        f"{path}: step {i} path {step_path!r} is absolute. Journey paths must be "
                        "origin-relative — an absolute one would send the identity's token to "
                        "that host."
                    )
                if not step_path.startswith("/"):
                    raise SystemExit(f"{path}: step {i} path {step_path!r} must start with '/'")
            steps.append(Step(phase=str(item.get("phase", f"{kind}{i}")), kind=kind, raw=item))
        return Journey(name=str(raw.get("name", path.stem)), steps=steps)


_STEP_KINDS = {"http", "chat_open", "chat_reopen", "turn", "ws_close", "sleep"}

# Frames that mean the agent itself has started producing something the
# viewer can see. Deliberately excludes ``ready``, which the manager emits
# on seating the socket whether or not a runner exists behind it.
_ACTIVITY_FRAMES = {
    "token",
    "tool_call",
    "tool_result",
    "assistant_message",
    # The browser renders these as cards the moment they arrive
    # (app/web/static/js/chat.js), so to the person watching, the screen has
    # stopped being blank. A turn whose first act is to ask for approval
    # would otherwise report no activity at all and then be blamed for a
    # first-token timeout the user never experienced as one.
    "approval_request",
    "question_request",
}


# ---------------------------------------------------------------------------
# Measurement sink
# ---------------------------------------------------------------------------


@dataclass
class Recorder:
    """Collects phase rows and writes them as JSONL.

    Rows are appended under a lock and flushed immediately, so a run killed
    mid-ramp still leaves every completed phase on disk — the data from an
    aborted step is exactly the data you most want.
    """

    path: Path
    rows: list[dict] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _fh: Any = None

    def open(self) -> None:
        """Create the output file, refusing to append to an existing one.

        The manifest summarises only the rows THIS invocation produced, so
        appending to a previous run's file leaves the data and its summary
        describing different things — with nothing in either saying so.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise SystemExit(
                f"{self.path} already exists. Its manifest would describe only the new rows "
                "while the file held both runs — pick a new --out."
            )
        self._fh = self.path.open("x")

    def close(self) -> None:
        if self._fh:
            self._fh.close()

    async def emit(self, row: dict) -> None:
        row.setdefault("ts", datetime.now(timezone.utc).isoformat())
        async with self._lock:
            self.rows.append(row)
            if self._fh:
                self._fh.write(json.dumps(row) + "\n")
                self._fh.flush()


# ---------------------------------------------------------------------------
# Virtual user
# ---------------------------------------------------------------------------


class CrossTalk(Exception):
    """A frame arrived stamped for a different chat_id."""


class Aborted(Exception):
    """The watcher tripped the abort file; stop starting new work."""


@dataclass
class UserContext:
    idx: int
    slug: str
    token: str
    base_url: str
    run_id: str
    step_label: str
    timeouts: dict
    recorder: Recorder
    verbose: bool = False
    abort_file: Optional[Path] = None
    chat_id: Optional[str] = None
    ws: Any = None
    #: Set when a turn ended without a `done` frame. The server may still be
    #: working, and its late frames arrive on this same socket carrying only a
    #: session sequence — nothing ties a frame to the turn that asked for it.
    #: Measuring the next turn on such a socket attributes the previous turn's
    #: output to it. (This is what produced ~0.2 ms "first activity" readings
    #: on reattached sockets in an early run.)
    turn_outstanding: bool = False
    _client: Optional[httpx.AsyncClient] = None

    def request_id(self, phase: str) -> str:
        # Sent as x-request-id and echoed back by the middleware, so a row
        # in this file and a line in Cloud Logging name each other.
        return f"lt-{self.run_id}-{self.step_label}-u{self.idx:02d}-{phase}"[:64]

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
                timeout=self.timeouts["http"],
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def base_row(self, step: Step) -> dict:
        return {
            "run_id": self.run_id,
            "step": self.step_label,
            "user": self.idx,
            "slug": self.slug,
            "phase": step.phase,
            "kind": step.kind,
            "chat_id": self.chat_id,
        }

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"[u{self.idx:02d}] {msg}", file=sys.stderr, flush=True)


async def _do_http(ctx: UserContext, step: Step) -> None:
    rid = ctx.request_id(step.phase)
    t0 = time.monotonic()
    row = ctx.base_row(step)
    row["request_id"] = rid
    try:
        resp = await ctx.client.request(
            str(step.raw.get("method", "GET")),
            step.path,
            headers={"x-request-id": rid},
        )
        ms = (time.monotonic() - t0) * 1000
        try:
            body: Any = resp.json() if resp.content and "json" in resp.headers.get("content-type", "") else ""
        except ValueError:
            body = ""
        row.update(
            ms=round(ms, 1),
            http_status=resp.status_code,
            bytes=len(resp.content),
            error=classify_http(resp.status_code, body),
            server_request_id=resp.headers.get("x-request-id"),
        )
    except Exception as exc:
        row.update(ms=round((time.monotonic() - t0) * 1000, 1), error="transport", detail=repr(exc)[:300])
    await ctx.recorder.emit(row)
    ctx.log(f"{step.phase} {row.get('ms')}ms {row.get('error') or 'ok'}")


async def _open_ws(ctx: UserContext, step: Step, ws_url: str) -> None:
    parsed = urlparse(ctx.base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    url = f"{scheme}://{parsed.netloc}{ws_url}"
    row = ctx.base_row(step)
    t0 = time.monotonic()
    try:
        ctx.ws = await asyncio.wait_for(
            ws_connect(url, additional_headers={"x-request-id": ctx.request_id(step.phase)}, max_size=None),
            timeout=ctx.timeouts["ws_open"],
        )
        row.update(ms=round((time.monotonic() - t0) * 1000, 1), error=None)
    except Exception as exc:
        row.update(ms=round((time.monotonic() - t0) * 1000, 1), error="ws_open_failed", detail=repr(exc)[:300])
        await ctx.recorder.emit(row)
        raise
    await ctx.recorder.emit(row)
    ctx.log(f"{step.phase} ws open {row['ms']}ms")


async def _do_chat_open(ctx: UserContext, step: Step) -> None:
    """POST /api/chat/sessions, then open the WS the response points at.

    Two phases are recorded because they fail for different reasons and at
    very different magnitudes: session create is a plain REST write, while
    the WS open is followed by a sandbox spawn whose completion is the
    ``ready`` frame measured on the first turn.
    """
    rid = ctx.request_id(step.phase)
    row = ctx.base_row(step)
    row["request_id"] = rid
    t0 = time.monotonic()
    try:
        resp = await ctx.client.post(
            "/api/chat/sessions",
            json={"surface": str(step.raw.get("surface", "web"))},
            headers={"x-request-id": rid},
        )
    except Exception as exc:
        # Without this the exception escapes before any row is written, and
        # the journey reports a generic failure — so connection refusals
        # under load, the very thing a ramp is looking for, vanish from the
        # per-phase error summary.
        row.update(ms=round((time.monotonic() - t0) * 1000, 1), error="transport", detail=repr(exc)[:300])
        await ctx.recorder.emit(row)
        raise
    ms = (time.monotonic() - t0) * 1000
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {"raw": resp.text[:300]}
    err = classify_http(resp.status_code, body)
    row.update(
        ms=round(ms, 1),
        http_status=resp.status_code,
        error=err,
        server_request_id=resp.headers.get("x-request-id"),
    )
    if err:
        row["detail"] = json.dumps(body)[:300]
        await ctx.recorder.emit(row)
        raise RuntimeError(f"chat_open failed: {resp.status_code} {err}")
    ctx.chat_id = str(body["id"])
    row["chat_id"] = ctx.chat_id
    await ctx.recorder.emit(row)
    ctx.log(f"{step.phase} session {ctx.chat_id} {row['ms']}ms")

    # A reopened socket is a fresh sink: whatever the old one had outstanding
    # cannot arrive on it as an unattributed frame.
    ctx.turn_outstanding = False
    await _open_ws(ctx, Step(phase=f"{step.phase}.ws_open", kind="ws_open", raw={}), str(body["ws_url"]))


async def _do_chat_reopen(ctx: UserContext, step: Step) -> None:
    """Mint a fresh ticket for the SAME session and re-attach.

    This is the path a person takes when they close the laptop and come
    back: the sandbox has been paused, so the ``ready`` frame on the next
    turn measures a resume rather than a spawn.
    """
    if not ctx.chat_id:
        raise RuntimeError("chat_reopen before chat_open")
    rid = ctx.request_id(step.phase)
    row = ctx.base_row(step)
    row["request_id"] = rid
    t0 = time.monotonic()
    try:
        resp = await ctx.client.post(
            f"/api/chat/sessions/{ctx.chat_id}/ticket",
            headers={"x-request-id": rid},
        )
    except Exception as exc:
        row.update(ms=round((time.monotonic() - t0) * 1000, 1), error="transport", detail=repr(exc)[:300])
        await ctx.recorder.emit(row)
        raise
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {"raw": resp.text[:300]}
    err = classify_http(resp.status_code, body)
    row.update(
        ms=round((time.monotonic() - t0) * 1000, 1),
        http_status=resp.status_code,
        error=err,
        server_request_id=resp.headers.get("x-request-id"),
    )
    if err:
        row["detail"] = json.dumps(body)[:300]
        await ctx.recorder.emit(row)
        raise RuntimeError(f"ticket failed: {resp.status_code} {err}")
    await ctx.recorder.emit(row)
    ctx.log(f"{step.phase} ticket {row['ms']}ms")
    # A reopened socket is a fresh sink: whatever the old one had outstanding
    # cannot arrive on it as an unattributed frame.
    ctx.turn_outstanding = False
    await _open_ws(ctx, Step(phase=f"{step.phase}.ws_open", kind="ws_open", raw={}), str(body["ws_url"]))


async def _do_turn(ctx: UserContext, step: Step) -> None:
    """Send one user_msg and consume frames until ``done``.

    Four timings, because on an agentic turn they answer four different
    questions and collapsing them misreads the system:

    ``ready``       the manager seated this socket. NOT the sandbox becoming
                    usable — the spawn path registers the session and returns
                    without awaiting the runner, so this lands in well under a
                    second even on a cold session and carries no information
                    about spawn cost.
    ``first_activity`` the first frame the RUNNER produced (a tool call, a
                    token, a whole message). This is the blank-screen wait a
                    person actually sits through, and the only timing that
                    contains the sandbox spawn — which is why it, not
                    ``first_token``, is what separates a cold turn from a warm
                    one.
    ``first_token`` the first token of prose. On a turn that searches before
                    it speaks this trails ``first_activity`` by however long
                    the tool work took, so it measures the QUESTION's depth as
                    much as the system's speed. Comparing it across turns with
                    different questions compares the questions.
    ``done``        the whole turn.

    A stream that ends without ``done`` is a failure, not a short success —
    the caller of a truncated turn got an incomplete answer and would have
    seen it as one.
    """
    if ctx.ws is None:
        raise RuntimeError("turn before an open WS")
    if ctx.turn_outstanding:
        # Refuse rather than produce a number that cannot be trusted. A
        # missing measurement is a gap; a wrong one is a false finding.
        await ctx.recorder.emit(
            {
                **ctx.base_row(step),
                "error": "skipped_outstanding_turn",
                "detail": "a previous turn on this socket never sent `done`; its late frames "
                "would be measured as this turn",
            }
        )
        ctx.log(f"{step.phase} SKIPPED — previous turn still outstanding")
        return
    client_msg_id = str(uuid.uuid4())
    row = ctx.base_row(step)
    row["client_msg_id"] = client_msg_id

    t0 = time.monotonic()
    await ctx.ws.send(json.dumps({"type": "user_msg", "text": step.text, "client_msg_id": client_msg_id}))

    ready_ms: Optional[float] = None
    first_activity_ms: Optional[float] = None
    first_token_ms: Optional[float] = None
    frames = 0
    tool_calls = 0
    chars = 0
    seqs: list[int] = []
    error: Optional[str] = None
    error_is_final = False
    detail: Optional[str] = None
    deadline_first = ctx.timeouts["first_token"]
    deadline_done = ctx.timeouts["turn_done"]
    deadline_done_default = deadline_done
    waiting_on: tuple[str, float] = ("turn_timeout", deadline_done_default)

    try:
        while True:
            elapsed_s = time.monotonic() - t0
            # Wait on the NEAREST live deadline, not always the whole-turn one.
            # A stream that goes silent before any prose must trip the
            # first-token budget at 240 s; blocking on the 420 s budget
            # instead mislabels it a turn_timeout and hides which budget the
            # server actually blew.
            deadlines = [(deadline_done - elapsed_s, "turn_timeout", deadline_done)]
            if first_token_ms is None:
                deadlines.append((deadline_first - elapsed_s, "first_token_timeout", deadline_first))
            remaining, kind, budget = min(deadlines)
            # Remembered for the except handler below: `wait_for` is what
            # actually raises, and it does not know which budget it was
            # counting down. Classifying there from a fixed name is how the
            # first-token budget got reported as a turn timeout.
            waiting_on = (kind, budget)
            if remaining <= 0:
                if not error_is_final:
                    error, detail = _timeout_reason(kind, budget)
                break
            raw = await asyncio.wait_for(ctx.ws.recv(), timeout=remaining)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            frames += 1
            elapsed = (time.monotonic() - t0) * 1000

            fid = frame.get("id")
            if isinstance(fid, str) and ctx.chat_id and not fid.startswith(f"{ctx.chat_id}:"):
                raise CrossTalk(f"frame {fid!r} on session {ctx.chat_id}")
            if isinstance(frame.get("seq"), int):
                seqs.append(int(frame["seq"]))

            ftype = frame.get("type")
            if ftype in _ACTIVITY_FRAMES and first_activity_ms is None:
                first_activity_ms = elapsed
            if ftype == "ready" and ready_ms is None:
                ready_ms = elapsed
            elif ftype == "tool_call":
                tool_calls += 1
            elif ftype == "token":
                if first_token_ms is None:
                    first_token_ms = elapsed
                chars += len(frame.get("text") or "")
            elif ftype == "assistant_message":
                if first_token_ms is None:
                    # A provider that does not stream still produced an
                    # answer; the perceived wait is this frame.
                    first_token_ms = elapsed
                chars = max(chars, len(frame.get("content") or ""))
            elif ftype == "error":
                # The server has stated why the turn failed. Keep waiting for
                # `done` so the transcript is complete, but this classification
                # is final: some refusals (sender limits) broadcast an error and
                # deliberately leave the socket attached with no `done` to
                # follow, and letting the later timeout overwrite this would
                # replace the server's own reason with our stopwatch's.
                error = "frame_error"
                error_is_final = True
                detail = json.dumps({k: frame.get(k) for k in ("kind", "message")})[:300]
            elif ftype == "done":
                break

    except CrossTalk:
        raise
    except asyncio.TimeoutError:
        if not error_is_final:
            error, detail = _timeout_reason(*waiting_on)
    except Exception as exc:
        if not error_is_final:
            error, detail = "stream_broken", repr(exc)[:300]

    done_ms = (time.monotonic() - t0) * 1000
    if error is None and frames and first_token_ms is None:
        error = "truncated"
    # `done` is the only frame that says the server finished with this turn.
    ctx.turn_outstanding = error is not None
    row.update(
        ready_ms=round(ready_ms, 1) if ready_ms is not None else None,
        first_activity_ms=round(first_activity_ms, 1) if first_activity_ms is not None else None,
        first_token_ms=round(first_token_ms, 1) if first_token_ms is not None else None,
        done_ms=round(done_ms, 1),
        ms=round(done_ms, 1),
        frames=frames,
        tool_calls=tool_calls,
        chars=chars,
        seq_gap=_seq_gap(seqs),
        error=error,
        detail=detail,
    )
    await ctx.recorder.emit(row)
    ctx.log(
        f"{step.phase} activity={row['first_activity_ms']} ttft={row['first_token_ms']} "
        f"done={row['done_ms']} tools={tool_calls} {row.get('error') or 'ok'}"
    )


def _timeout_reason(kind: str, budget: float) -> tuple[str, str]:
    """Name the budget that ran out, so the row says which limit was reached."""
    what = "token" if kind.startswith("first") else "done frame"
    return kind, f"no {what} within {budget}s"


def _seq_gap(seqs: list[int]) -> Optional[int]:
    """Frames missing between the lowest and highest seq we saw.

    The client can only detect a gap it is told about; a non-zero value
    here means frames were dropped between us and the manager.
    """
    if len(seqs) < 2:
        return None
    ordered = sorted(seqs)
    return (ordered[-1] - ordered[0] + 1) - len(set(ordered))


async def _do_ws_close(ctx: UserContext, step: Step) -> None:
    row = ctx.base_row(step)
    t0 = time.monotonic()
    if ctx.ws is not None:
        try:
            await ctx.ws.close()
        except Exception as exc:
            row["detail"] = repr(exc)[:200]
        ctx.ws = None
    row.update(ms=round((time.monotonic() - t0) * 1000, 1), error=None)
    await ctx.recorder.emit(row)
    ctx.log(f"{step.phase} closed")


async def _do_sleep(ctx: UserContext, step: Step) -> None:
    ctx.log(f"{step.phase} sleeping {step.seconds}s")
    await asyncio.sleep(step.seconds)
    await ctx.recorder.emit({**ctx.base_row(step), "ms": step.seconds * 1000, "error": None})


_HANDLERS = {
    "http": _do_http,
    "chat_open": _do_chat_open,
    "chat_reopen": _do_chat_reopen,
    "turn": _do_turn,
    "ws_close": _do_ws_close,
    "sleep": _do_sleep,
}


async def run_user(ctx: UserContext, journey: Journey, start_delay: float) -> dict:
    """One virtual user's whole journey. Never raises — reports instead.

    A failing user must not take the step down with it: the run's job is to
    find out how many of N succeed, which is unanswerable if the first
    failure aborts the gather.
    """
    await asyncio.sleep(start_delay)
    outcome: dict = {"user": ctx.idx, "slug": ctx.slug, "completed": False, "failed_phase": None, "error": None}
    try:
        for step in journey.steps:
            # Between steps, not inside a turn: cutting a turn mid-stream
            # would record a truncation the server never caused. The point
            # of the abort is to stop ADDING load, and a journey has enough
            # steps that this reacts within seconds.
            if ctx.abort_file is not None and ctx.abort_file.exists():
                raise Aborted(ctx.abort_file.read_text().strip()[:200])
            await _HANDLERS[step.kind](ctx, step)
        # Reaching the last step is NOT success. A turn that times out or
        # returns an error frame records its failure and the journey walks
        # on to the next step, so "we got to the end" and "it worked" are
        # different questions — and reporting the first as the second is how
        # a 27%-failure run reported ten clean journeys.
        failed = [str(r.get("phase")) for r in ctx.recorder.rows if r.get("user") == ctx.idx and r.get("error")]
        outcome["completed"] = not failed
        if failed:
            outcome["failed_phase"] = ",".join(failed)
            outcome["error"] = f"{len(failed)} phase(s) failed"
    except Aborted as exc:
        outcome.update(failed_phase="aborted", error=str(exc))
        await ctx.recorder.emit({**ctx.base_row(Step("aborted", "abort", {})), "error": "aborted", "detail": str(exc)})
    except CrossTalk as exc:
        outcome.update(failed_phase="cross_talk", error=str(exc))
        await ctx.recorder.emit(
            {**ctx.base_row(Step("cross_talk", "assert", {})), "error": "cross_talk", "detail": str(exc)}
        )
    except Exception as exc:
        outcome.update(failed_phase="journey", error=repr(exc)[:300])
    finally:
        await ctx.aclose()
    return outcome


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/").lower()


def _load_identities(path: Path, count: int, base_url: str, *, allow_host_mismatch: bool = False) -> list[dict]:
    """Load N identities, refusing to send their PATs to the wrong host.

    The state file records the instance the tokens were minted against.
    Nothing else stops a mistyped or stale ``--base-url`` from presenting
    every one of them to a different server, which is a credential
    disclosure and not a failed run.
    """
    raw = json.loads(path.read_text())
    minted_for = str(raw.get("base_url") or "")
    if minted_for and _origin(minted_for) != _origin(base_url) and not allow_host_mismatch:
        raise SystemExit(
            f"{path} holds tokens minted for {_origin(minted_for)}, but --base-url is "
            f"{_origin(base_url)}. Refusing to send them there. Pass --allow-host-mismatch "
            "only if you are certain the same credentials are valid on both."
        )
    identities = raw["identities"]
    if len(identities) < count:
        raise SystemExit(f"{path} holds {len(identities)} identities, --users asks for {count}")
    missing = [i["slug"] for i in identities[:count] if not i.get("token")]
    if missing:
        raise SystemExit(
            f"{path} has identities with no token ({', '.join(missing)}) — a partial "
            "provisioning run. Tear it down and re-provision."
        )
    return identities[:count]


def summarize(rows: list[dict]) -> dict:
    """p50/p95 per phase plus the error histogram.

    p95 on fewer than 20 samples is reported anyway but is a shape, not a
    number — the ramp's small steps exist to be looked at, not to be
    quoted.
    """
    by_phase: dict[str, dict[str, list[float]]] = {}
    errors: dict[str, int] = {}
    for row in rows:
        phase = str(row.get("phase"))
        bucket = by_phase.setdefault(phase, {})
        for metric in ("ms", "ready_ms", "first_activity_ms", "first_token_ms", "done_ms"):
            value = row.get(metric)
            if isinstance(value, (int, float)):
                bucket.setdefault(metric, []).append(float(value))
        if row.get("error"):
            errors[str(row["error"])] = errors.get(str(row["error"]), 0) + 1

    out: dict[str, Any] = {"phases": {}, "errors": errors}
    for phase, metrics in by_phase.items():
        out["phases"][phase] = {
            metric: {
                "n": len(values),
                "p50": round(statistics.median(values), 1),
                "p95": round(sorted(values)[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))], 1),
                "max": round(max(values), 1),
            }
            for metric, values in sorted(metrics.items())
        }
    return out


async def run_step(args: argparse.Namespace) -> int:
    if ws_connect is None:
        raise SystemExit("websockets is not installed — pip install websockets")

    journey = Journey.load(Path(args.journey))
    identities = _load_identities(
        Path(args.identities), args.users, args.base_url, allow_host_mismatch=args.allow_host_mismatch
    )
    if args.abort_file and Path(args.abort_file).exists():
        raise SystemExit(f"{args.abort_file} exists — a previous step aborted. Read it, then delete it to run again.")
    run_id = args.run_id or uuid.uuid4().hex[:8]
    step_label = args.step_label or f"n{args.users}"

    recorder = Recorder(path=Path(args.out))
    recorder.open()
    started = datetime.now(timezone.utc)
    print(
        f"[run] id={run_id} step={step_label} users={args.users} journey={journey.name} "
        f"stagger={args.stagger}s start={started.isoformat()}",
        file=sys.stderr,
    )

    contexts = [
        UserContext(
            idx=ident["idx"],
            slug=ident["slug"],
            token=ident["token"],
            base_url=args.base_url.rstrip("/"),
            run_id=run_id,
            step_label=step_label,
            timeouts=DEFAULT_TIMEOUTS,
            recorder=recorder,
            verbose=args.verbose,
            abort_file=Path(args.abort_file) if args.abort_file else None,
        )
        for ident in identities
    ]
    # Even spread rather than a simultaneous release: a room of people does
    # not press enter at the same instant, and a thundering herd measures
    # the herd instead of the room.
    gap = (args.stagger / max(len(contexts) - 1, 1)) if len(contexts) > 1 else 0.0
    outcomes = await asyncio.gather(
        *(run_user(ctx, journey, i * gap) for i, ctx in enumerate(contexts)),
        return_exceptions=False,
    )
    ended = datetime.now(timezone.utc)
    recorder.close()

    manifest = {
        "run_id": run_id,
        "step": step_label,
        "journey": journey.name,
        "users": args.users,
        "base_url": args.base_url,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "duration_s": round((ended - started).total_seconds(), 1),
        "outcomes": outcomes,
        "summary": summarize(recorder.rows),
    }
    manifest_path = Path(args.out).with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2))

    completed = sum(1 for o in outcomes if o["completed"])
    aborted = sum(1 for o in outcomes if o["failed_phase"] == "aborted")
    if aborted:
        print(f"[run] ABORTED — {aborted}/{len(outcomes)} journeys stopped early", file=sys.stderr)
    print(json.dumps(manifest["summary"], indent=2), file=sys.stderr)
    print(
        f"[run] {completed}/{len(outcomes)} journeys completed; "
        f"window {started.isoformat()} .. {ended.isoformat()}; "
        f"rows -> {args.out}; manifest -> {manifest_path}",
        file=sys.stderr,
    )
    return 0 if completed == len(outcomes) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", required=True)
    p.add_argument("--identities", required=True, help="state file from provision.py")
    p.add_argument("--journey", required=True, help="journey YAML")
    p.add_argument("--users", type=int, required=True, help="virtual users in THIS ramp step")
    p.add_argument("--stagger", type=float, default=30.0, help="seconds to spread user starts over")
    p.add_argument("--out", required=True, help="JSONL output path")
    p.add_argument("--run-id", default=None)
    p.add_argument("--step-label", default=None, help="e.g. ramp5 — tags every row")
    p.add_argument(
        "--abort-file",
        default=None,
        help="stop each journey at its next step once this file exists (written by watch.py)",
    )
    p.add_argument(
        "--allow-host-mismatch",
        action="store_true",
        help="send the state file's PATs to a --base-url they were not minted for",
    )
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="validate journey + identities, then exit")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run:
        journey = Journey.load(Path(args.journey))
        identities = _load_identities(
            Path(args.identities), args.users, args.base_url, allow_host_mismatch=args.allow_host_mismatch
        )
        print(f"journey {journey.name}: {len(journey.steps)} steps", file=sys.stderr)
        for s in journey.steps:
            extra = s.path if s.kind == "http" else (f"{s.seconds}s" if s.kind == "sleep" else "")
            preview = (s.text[:60] + "...") if s.kind == "turn" else extra
            print(f"  {s.phase:<24} {s.kind:<12} {preview}", file=sys.stderr)
        print(f"identities: {len(identities)} ({', '.join(i['slug'] for i in identities)})", file=sys.stderr)
        return 0
    return asyncio.run(run_step(args))


if __name__ == "__main__":
    raise SystemExit(main())
