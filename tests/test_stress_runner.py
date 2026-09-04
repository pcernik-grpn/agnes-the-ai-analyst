"""The stress runner drives the real chat protocol — verified against a stub.

The runner's whole value is that its numbers describe what a browser would
have experienced, so the thing worth testing is that it reads the protocol
correctly: which frame stops the clock for each of the three timings, what
counts as a finished turn, and what it does when the stream lies. Proving
that against a stub costs a second; discovering it against a live customer
instance costs a spoiled run.

The stub speaks the same contract as ``app/api/chat.py``: create returns a
ticketed ``ws_url``, the socket emits ``ready`` (after a delay standing in
for the sandbox spawn) then ``token`` deltas then ``done``, and every frame
is stamped ``id = f"{chat_id}:{seq}"``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest

uvicorn = pytest.importorskip("uvicorn")
fastapi = pytest.importorskip("fastapi")

from fastapi import FastAPI, WebSocket  # noqa: E402
from fastapi.responses import JSONResponse, PlainTextResponse  # noqa: E402

from scripts.stress.runner import Journey, classify_http, main, run_step  # noqa: E402


# ---------------------------------------------------------------------------
# Stub instance
# ---------------------------------------------------------------------------

READY_DELAY_S = 0.15
TOKEN_DELAY_S = 0.10


def _build_stub(behavior: dict) -> FastAPI:
    app = FastAPI()
    state: dict = {"sessions": {}, "seq": {}}

    @app.get("/")
    async def index() -> PlainTextResponse:
        return PlainTextResponse("dashboard")

    @app.get("/catalog")
    async def catalog() -> PlainTextResponse:
        return PlainTextResponse("catalog")

    @app.post("/api/chat/sessions", status_code=201)
    async def create_session() -> JSONResponse:
        if behavior.get("create_status"):
            return JSONResponse({"detail": behavior.get("create_body", {})}, status_code=behavior["create_status"])
        chat_id = uuid.uuid4().hex[:12]
        ticket = uuid.uuid4().hex
        state["sessions"][chat_id] = ticket
        state["seq"][chat_id] = 0
        return JSONResponse(
            {"id": chat_id, "ws_ticket": ticket, "ws_url": f"/api/chat/sessions/{chat_id}/stream?ticket={ticket}"},
            status_code=201,
        )

    @app.post("/api/chat/sessions/{chat_id}/ticket", status_code=201)
    async def reissue(chat_id: str) -> JSONResponse:
        ticket = uuid.uuid4().hex
        state["sessions"][chat_id] = ticket
        return JSONResponse(
            {"id": chat_id, "ws_ticket": ticket, "ws_url": f"/api/chat/sessions/{chat_id}/stream?ticket={ticket}"},
            status_code=201,
        )

    def _stamp(chat_id: str, frame: dict) -> dict:
        state["seq"][chat_id] = state["seq"].get(chat_id, 0) + 1
        seq = state["seq"][chat_id]
        return {**frame, "seq": seq, "id": f"{chat_id}:{seq}"}

    @app.websocket("/api/chat/sessions/{chat_id}/stream")
    async def stream(ws: WebSocket, chat_id: str) -> None:
        await ws.accept()
        try:
            while True:
                raw = await ws.receive_text()
                frame = json.loads(raw)
                if frame.get("type") != "user_msg":
                    continue
                await asyncio.sleep(READY_DELAY_S)
                stamp_id = behavior.get("wrong_chat_id") or chat_id
                await ws.send_json(_stamp(stamp_id, {"type": "ready"}))
                if behavior.get("emit_error"):
                    await ws.send_json(_stamp(chat_id, {"type": "error", "kind": "boom", "message": "nope"}))
                    await ws.send_json(_stamp(chat_id, {"type": "done"}))
                    continue
                # A real runner does not start working the instant the
                # socket is seated, and does not speak the instant it starts
                # working — the two delays keep those three moments
                # distinguishable so the assertions mean something.
                await asyncio.sleep(TOKEN_DELAY_S)
                await ws.send_json(_stamp(chat_id, {"type": "tool_call", "name": "search"}))
                await asyncio.sleep(TOKEN_DELAY_S)
                for piece in ("Yes", " — two", " precedents."):
                    await ws.send_json(_stamp(chat_id, {"type": "token", "text": piece}))
                if behavior.get("truncate"):
                    await ws.close()
                    return
                await ws.send_json(_stamp(chat_id, {"type": "done"}))
        except Exception:
            return

    return app


@pytest.fixture
def stub_server():
    """Start the stub on a free port; yields (base_url, behavior dict).

    Behavior is mutated by the test BEFORE the runner is invoked, so one
    fixture covers the happy path and each failure mode without a second
    server.
    """
    behavior: dict = {}
    app = _build_stub(behavior)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started:
        if time.time() > deadline:  # pragma: no cover — CI stall guard
            raise RuntimeError("stub server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}", behavior
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# ---------------------------------------------------------------------------
# Fixtures for runner inputs
# ---------------------------------------------------------------------------


def _write_journey(tmp_path: Path, *, idle_seconds: float = 0.2) -> Path:
    path = tmp_path / "journey.yaml"
    path.write_text(
        "name: stub\n"
        "steps:\n"
        "  - {phase: dashboard, kind: http, method: GET, path: /}\n"
        "  - {phase: catalog, kind: http, method: GET, path: /catalog}\n"
        "  - {phase: session_create, kind: chat_open}\n"
        "  - {phase: turn_cold, kind: turn, text: first question}\n"
        "  - {phase: turn_warm, kind: turn, text: follow up}\n"
        "  - {phase: detach, kind: ws_close}\n"
        f"  - {{phase: idle, kind: sleep, seconds: {idle_seconds}}}\n"
        "  - {phase: reattach, kind: chat_reopen}\n"
        "  - {phase: turn_resumed, kind: turn, text: third question}\n"
        "  - {phase: close, kind: ws_close}\n"
    )
    return path


def _write_identities(tmp_path: Path, count: int) -> Path:
    path = tmp_path / "identities.json"
    path.write_text(
        json.dumps(
            {
                "base_url": "http://stub",
                "group_name": "loadtest",
                "group_id": "g1",
                "grant_ids": [],
                "identities": [
                    {
                        "idx": i,
                        "slug": f"loadbot-{i:02d}",
                        "account_id": f"acc{i}",
                        "email": f"loadbot-{i:02d}@service.local",
                        "token": f"tok{i}",
                    }
                    for i in range(1, count + 1)
                ],
            }
        )
    )
    return path


def _args(
    base_url: str, tmp_path: Path, *, users: int, journey: Path, abort_file: str | None = None
) -> argparse.Namespace:
    return argparse.Namespace(
        base_url=base_url,
        identities=str(_write_identities(tmp_path, users)),
        journey=str(journey),
        users=users,
        stagger=0.0,
        out=str(tmp_path / "run.jsonl"),
        run_id="test",
        step_label="stub",
        abort_file=abort_file,
        verbose=False,
        dry_run=False,
    )


def _rows(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "run.jsonl").read_text().splitlines()]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_journey_completes_and_records_every_phase(stub_server, tmp_path):
    base_url, _ = stub_server
    rc = asyncio.run(run_step(_args(base_url, tmp_path, users=1, journey=_write_journey(tmp_path))))
    assert rc == 0

    rows = _rows(tmp_path)
    phases = [r["phase"] for r in rows]
    assert phases == [
        "dashboard",
        "catalog",
        "session_create",
        "session_create.ws_open",
        "turn_cold",
        "turn_warm",
        "detach",
        "idle",
        "reattach",
        "reattach.ws_open",
        "turn_resumed",
        "close",
    ]
    assert all(r.get("error") is None for r in rows), [r for r in rows if r.get("error")]


def test_three_turns_yield_three_independent_ttft_numbers(stub_server, tmp_path):
    """The headline measurement: cold / warm / resumed are separate rows.

    A journey that reported one number for all three could not answer
    whether the agent process is per-session or per-turn, which is the
    question the extra follow-up turn exists to settle.
    """
    base_url, _ = stub_server
    asyncio.run(run_step(_args(base_url, tmp_path, users=1, journey=_write_journey(tmp_path))))
    turns = {r["phase"]: r for r in _rows(tmp_path) if r["kind"] == "turn"}

    assert set(turns) == {"turn_cold", "turn_warm", "turn_resumed"}
    for row in turns.values():
        assert row["first_token_ms"] is not None
        assert row["done_ms"] >= row["first_token_ms"]
        # The stub emits `ready` on every turn, unlike a live warm turn —
        # what is asserted here is that the runner records it per turn, not
        # that a live warm turn has one.
        assert row["ready_ms"] is not None
        assert row["frames"] >= 5  # ready + tool_call + 3 tokens + done
        assert row["tool_calls"] == 1


def test_first_activity_precedes_first_token_when_the_agent_searches(stub_server, tmp_path):
    """The blank-screen wait and the wait for prose are different numbers.

    The stub emits a tool call before any token, exactly as a retrieval turn
    does. Reporting only first_token on such a turn measures how much work
    the question needed and calls it latency — the confusion that made a
    warm follow-up look five times slower than a cold turn in the first
    live run.
    """
    base_url, _ = stub_server
    asyncio.run(run_step(_args(base_url, tmp_path, users=1, journey=_write_journey(tmp_path))))

    for row in (r for r in _rows(tmp_path) if r["kind"] == "turn"):
        assert row["first_activity_ms"] is not None
        assert row["first_activity_ms"] < row["first_token_ms"]
        # `ready` is not activity: the manager sends it on seating the
        # socket, before any runner has produced anything.
        assert row["first_activity_ms"] > row["ready_ms"]


def test_truncated_stream_is_a_failure_not_a_short_success(stub_server, tmp_path):
    base_url, behavior = stub_server
    behavior["truncate"] = True
    rc = asyncio.run(run_step(_args(base_url, tmp_path, users=1, journey=_write_journey(tmp_path))))
    assert rc == 1

    cold = next(r for r in _rows(tmp_path) if r["phase"] == "turn_cold")
    assert cold["error"] == "stream_broken"


def test_error_frame_is_captured_verbatim(stub_server, tmp_path):
    base_url, behavior = stub_server
    behavior["emit_error"] = True
    asyncio.run(run_step(_args(base_url, tmp_path, users=1, journey=_write_journey(tmp_path))))

    cold = next(r for r in _rows(tmp_path) if r["phase"] == "turn_cold")
    assert cold["error"] == "frame_error"
    assert "boom" in cold["detail"]


def test_cross_talk_aborts_the_user_and_is_recorded(stub_server, tmp_path):
    """A frame stamped for another session must never be counted as ours.

    Checking the ``id`` envelope rather than the answer text makes this
    independent of what the model said, so it holds for real questions
    with no injected marker.
    """
    base_url, behavior = stub_server
    behavior["wrong_chat_id"] = "someone-else"
    rc = asyncio.run(run_step(_args(base_url, tmp_path, users=1, journey=_write_journey(tmp_path))))
    assert rc == 1
    assert any(r.get("error") == "cross_talk" for r in _rows(tmp_path))


def test_session_create_refusals_are_classified_not_swallowed(stub_server, tmp_path):
    base_url, behavior = stub_server
    behavior["create_status"] = 429
    behavior["create_body"] = {"kind": "concurrency_cap", "hint": "3 live sessions"}
    rc = asyncio.run(run_step(_args(base_url, tmp_path, users=1, journey=_write_journey(tmp_path))))
    assert rc == 1

    row = next(r for r in _rows(tmp_path) if r["phase"] == "session_create")
    assert row["error"] == "concurrency_cap"


def test_concurrent_users_each_get_their_own_session(stub_server, tmp_path):
    base_url, _ = stub_server
    rc = asyncio.run(run_step(_args(base_url, tmp_path, users=4, journey=_write_journey(tmp_path))))
    assert rc == 0

    chat_ids = {r["chat_id"] for r in _rows(tmp_path) if r["kind"] == "turn"}
    assert len(chat_ids) == 4


def test_manifest_carries_the_utc_window_and_summary(stub_server, tmp_path):
    base_url, _ = stub_server
    asyncio.run(run_step(_args(base_url, tmp_path, users=2, journey=_write_journey(tmp_path))))

    manifest = json.loads((tmp_path / "run.manifest.json").read_text())
    assert manifest["started_at"] < manifest["ended_at"]
    assert manifest["users"] == 2
    assert manifest["summary"]["phases"]["turn_cold"]["first_token_ms"]["n"] == 2
    assert manifest["summary"]["phases"]["turn_cold"]["first_activity_ms"]["n"] == 2
    assert manifest["summary"]["errors"] == {}


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (201, {}, None),
        (429, {"detail": {"kind": "concurrency_cap"}}, "concurrency_cap"),
        (429, {"detail": "budget_exhausted"}, "budget_exhausted"),
        (403, {}, "chat_access_denied"),
        (503, {"detail": "chat_disabled"}, "chat_disabled"),
        (500, {"detail": "QueuePool limit of size 5 overflow 10"}, "pool_exhausted"),
        (500, {}, "server_error_maybe_sender_limit"),
    ],
)
def test_http_classification(status, body, expected):
    assert classify_http(status, body) == expected


def test_journey_rejects_an_unknown_step_kind(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("name: x\nsteps:\n  - {phase: p, kind: teleport}\n")
    with pytest.raises(SystemExit, match="unknown kind"):
        Journey.load(path)


def test_dry_run_validates_without_touching_the_network(tmp_path, capsys):
    journey = _write_journey(tmp_path)
    identities = _write_identities(tmp_path, 2)
    rc = main(
        [
            "--base-url",
            "http://unreachable.invalid",
            "--identities",
            str(identities),
            "--journey",
            str(journey),
            "--users",
            "2",
            "--out",
            str(tmp_path / "unused.jsonl"),
            "--dry-run",
        ]
    )
    assert rc == 0
    assert not (tmp_path / "unused.jsonl").exists()


def test_dry_run_refuses_more_users_than_identities(tmp_path):
    with pytest.raises(SystemExit, match="identities"):
        main(
            [
                "--base-url",
                "http://unreachable.invalid",
                "--identities",
                str(_write_identities(tmp_path, 2)),
                "--journey",
                str(_write_journey(tmp_path)),
                "--users",
                "5",
                "--out",
                str(tmp_path / "unused.jsonl"),
                "--dry-run",
            ]
        )


def test_abort_file_appearing_mid_run_stops_journeys_at_the_next_step(stub_server, tmp_path):
    """An abort raised DURING a run stops adding load and says so in the data.

    Written while the journeys are in their idle step, which is where a
    real abort lands: the watcher trips on a signal from the server, not in
    lockstep with the runner. Distinct from the stale-file case below —
    that one must refuse to start at all.
    """
    base_url, _ = stub_server
    abort = tmp_path / "ABORT"
    journey = _write_journey(tmp_path, idle_seconds=2.0)

    async def drive() -> int:
        async def trip() -> None:
            await asyncio.sleep(1.0)
            abort.write_text("CPU throttling >= 35% of scheduling periods\n")

        rc, _ = await asyncio.gather(
            run_step(_args(base_url, tmp_path, users=2, journey=journey, abort_file=str(abort))),
            trip(),
        )
        return rc

    assert asyncio.run(drive()) == 1

    rows = _rows(tmp_path)
    aborted = [r for r in rows if r.get("error") == "aborted"]
    assert len(aborted) == 2, "both journeys should have stopped"
    # It tripped during the idle step, so the two turns before it ran and
    # the one after it did not — the abort stops new work, it does not
    # retroactively fail work already done.
    assert {r["phase"] for r in rows if r["kind"] == "turn"} == {"turn_cold", "turn_warm"}
    assert all(r.get("error") is None for r in rows if r["kind"] == "turn")

    manifest = json.loads((tmp_path / "run.manifest.json").read_text())
    assert all(o["failed_phase"] == "aborted" for o in manifest["outcomes"])


def test_a_stale_abort_file_refuses_to_start(stub_server, tmp_path):
    base_url, _ = stub_server
    abort = tmp_path / "ABORT"
    abort.write_text("from the previous ramp step\n")
    with pytest.raises(SystemExit, match="aborted"):
        asyncio.run(
            run_step(_args(base_url, tmp_path, users=1, journey=_write_journey(tmp_path), abort_file=str(abort)))
        )


def test_a_failed_turn_makes_the_journey_not_completed(stub_server, tmp_path):
    """Reaching the last step is not success.

    The runner walks on past a failed turn by design (one bad answer must
    not cost the remaining measurements), so the manifest has to distinguish
    "finished the journey" from "the journey worked" — otherwise a run with
    failing turns reports every outcome clean, which is exactly how a
    27%-failure wave read as ten good journeys.
    """
    base_url, behavior = stub_server
    behavior["emit_error"] = True

    rc = asyncio.run(run_step(_args(base_url, tmp_path, users=2, journey=_write_journey(tmp_path))))
    assert rc == 1

    manifest = json.loads((tmp_path / "run.manifest.json").read_text())
    assert all(o["completed"] is False for o in manifest["outcomes"])
    for o in manifest["outcomes"]:
        assert "turn_cold" in o["failed_phase"]
        assert "phase(s) failed" in o["error"]
    # The journey still ran to the end — every phase is on disk.
    assert {r["phase"] for r in _rows(tmp_path)} >= {"turn_resumed", "close"}
