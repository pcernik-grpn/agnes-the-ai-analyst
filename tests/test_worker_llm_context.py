"""The worker binds the job id into the LLM call context.

Every generation a handler runs inside a job should be attributable to that
job with no per-handler code — the same contract ``bind_request_id`` already
gives the request id. Driven through the real ``worker_loop`` (the fixture
pattern of ``tests/test_worker_runtime.py``) rather than by calling the
private ``_run_one``, because what matters is that the binding survives the
``asyncio.to_thread`` hop the handler actually runs on.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from src.observability.llm_context import current_llm_context


@pytest.fixture
def worker_db(tmp_path, monkeypatch):
    """Fresh system.duckdb under a tmp DATA_DIR, closed after the test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    from src.db import close_system_db, get_system_db

    get_system_db()  # forces schema creation (incl. the jobs table)
    yield
    close_system_db()


@pytest.fixture(autouse=True)
def clean_job_kinds_registry():
    """The registry is a process-wide module dict — isolate each test."""
    from app.worker.registry import JOB_KINDS

    JOB_KINDS.clear()
    yield
    JOB_KINDS.clear()


async def _run_and_cancel(coro, duration_s: float) -> None:
    task = asyncio.create_task(coro)
    await asyncio.sleep(duration_s)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_the_handler_sees_the_job_id_in_the_llm_context(worker_db):
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    seen: list[str | None] = []

    def handler(payload: dict) -> None:
        seen.append(current_llm_context().job_id)

    register_kind(JobKind(name="llm_ctx_test", handler=handler, lane=LIGHT_LANE, lease_seconds=30))

    repo = jobs_repo()
    job = repo.enqueue("llm_ctx_test", {})

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.6))

    assert seen, "the handler never ran"
    assert seen[0] == str(job["id"])


def test_the_job_id_does_not_leak_past_the_job(worker_db):
    """The binding is reset in the same ``finally`` that resets the request
    id, so the next claim in this slot does not inherit it."""
    from app.worker.registry import LIGHT_LANE, JobKind, register_kind
    from app.worker.runtime import worker_loop
    from src.repositories import jobs_repo

    def handler(payload: dict) -> None:
        return None

    register_kind(JobKind(name="llm_ctx_leak_test", handler=handler, lane=LIGHT_LANE, lease_seconds=30))

    repo = jobs_repo()
    repo.enqueue("llm_ctx_leak_test", {})

    asyncio.run(_run_and_cancel(worker_loop(worker_id="test-worker", poll_interval_s=0.05), 0.6))

    assert repo.list(kind="llm_ctx_leak_test", status="done"), "the job never completed"
    assert current_llm_context().job_id is None
