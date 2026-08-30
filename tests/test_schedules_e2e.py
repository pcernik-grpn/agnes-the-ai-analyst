"""In-process schedules E2E — proves a scheduled job actually fires.

`tests/test_scheduler.py` proves `services/scheduler/__main__.py`'s job
list/cadence logic and `_run_job`'s bookkeeping in isolation (`_call_api` is
monkeypatched away entirely, so no HTTP ever happens).
`tests/test_agent_schedules_api.py` proves `POST /api/v1/agents/run-due`'s
dispatch logic via `TestClient` (no scheduler involved). Neither proves the
two actually connect: that the scheduler's *real* `_run_job` -> `_call_api`
path, sending real HTTP over a real loopback socket with the real
`SCHEDULER_API_TOKEN` shared-secret header, reaches a live app and produces
the same observable side effect an operator would see in production — a due
agent schedule gets claimed and its job enqueued.

No external credentials required: the "live" server here is this same
process's FastAPI app bound to a loopback TCP port via uvicorn, mirroring
`tests/test_marketplace_server_git.py::git_live_server`.

Run with: pytest tests/test_schedules_e2e.py -v
"""

from __future__ import annotations

import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import uvicorn

# Spawns real threads and binds a real network socket — the repo's `slow`
# marker for exactly this shape (`pytest.ini`: "tests that spawn real
# subprocesses or bind network sockets"). Unlike `live`/`docker`, `slow` is
# NOT deselected by default, so this test runs in normal CI.
pytestmark = pytest.mark.slow


@pytest.fixture
def live_app_server(shared_app):
    """Bind `shared_app` to a real loopback TCP socket via uvicorn in a
    background thread, so the scheduler can speak real HTTP against it
    instead of a `TestClient` in-process call.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(shared_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("live uvicorn server did not start in time")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def due_agent_schedule(e2e_env):
    """An enabled agent schedule that is unambiguously due: its cadence
    anchor is pushed an hour into the past, well clear of any `every 1m`
    boundary — deterministic, no reliance on wall-clock timing windows.
    """
    from src.db import get_system_db
    from src.repositories import agent_schedules_repo, agents_repo
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="sched-owner1", email="sched-owner@test.com", name="Owner")
    conn.close()

    agent_id = str(uuid.uuid4())
    agents_repo().create(id=agent_id, owner_user_id="sched-owner1", name="Briefing Bot", slug="e5-briefing-bot")

    schedule_id = str(uuid.uuid4())
    repo = agent_schedules_repo()
    repo.create(
        id=schedule_id,
        agent_id=agent_id,
        name="morning-briefing",
        schedule="every 1m",
        prompt="say hi",
        enabled=True,
    )
    row = repo.get(schedule_id)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert repo.claim_for_run(schedule_id, row["last_run_at"], past) is True

    return {"agent_id": agent_id, "schedule_id": schedule_id}


def test_scheduler_tick_fires_agents_run_due_over_real_http(due_agent_schedule, live_app_server, monkeypatch):
    """One real scheduler tick: production `_run_job` -> `_call_api` -> real
    HTTP (loopback socket) -> live app -> `POST /api/v1/agents/run-due` ->
    `_dispatch_if_due` claims the due row and enqueues its job.

    `API_URL` and `SCHEDULER_API_TOKEN` are module-level constants in
    `services.scheduler.__main__`, captured once from the environment at
    import time (not re-read per call) — so pointing the scheduler at the
    live loopback server requires patching the module attributes directly,
    not just `monkeypatch.setenv`. The app-side validator
    (`app.auth.scheduler_token.get_scheduler_secret`) DOES re-read the env
    var on every call, so `monkeypatch.setenv` is still what makes the
    live app accept the token.
    """
    from services.scheduler import __main__ as sched
    from src.repositories import agent_schedules_repo, jobs_repo

    token = "e5-schedules-live-test-shared-secret-32chars!!"
    monkeypatch.setenv("SCHEDULER_API_TOKEN", token)  # app-side: read fresh per call
    monkeypatch.setattr(sched, "SCHEDULER_API_TOKEN", token)  # scheduler-side: frozen at import
    monkeypatch.setattr(sched, "API_URL", live_app_server)  # ditto

    jobs = sched.build_jobs()
    job = next(j for j in jobs if j[0] == "agents:run-due")
    name, _schedule_str, endpoint, method, timeout_sec = job[:5]
    assert (endpoint, method) == ("/api/v1/agents/run-due", "POST")

    last_run: dict[str, str | None] = {name: None}
    in_flight: set[str] = set()
    lock = threading.Lock()
    now_iso = datetime.now(timezone.utc).isoformat()

    # Exactly what run()'s tick loop invokes (via executor.submit) once
    # is_table_due() says a job is due — called directly here for a
    # synchronous, deterministic assertion point.
    sched._run_job(name, endpoint, method, timeout_sec, now_iso, last_run, in_flight, lock)

    # Bookkeeping ran to completion on the real path (not skipped/errored).
    assert last_run[name] == now_iso
    assert name not in in_flight

    # The side effect an operator would see in production: the due row got
    # claimed and its job enqueued — proof the HTTP call actually landed on
    # the live app and `_dispatch_if_due` ran.
    row = agent_schedules_repo().get(due_agent_schedule["schedule_id"])
    assert row["last_status"] == "enqueued", row
    assert row["last_job_id"]

    job_row = jobs_repo().get(row["last_job_id"])
    assert job_row is not None
    assert job_row["kind"] == "agent_response"
    assert job_row["payload_json"]["agent_id"] == due_agent_schedule["agent_id"]
