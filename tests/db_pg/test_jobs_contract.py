"""Cross-engine contract tests for the ``jobs`` repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Foundation for the wave-2B
worker runtime — this file now also covers the claim/lease/complete/fail
lifecycle (the worker loop itself is a later task).

Follows the pattern established in ``test_ticket_contract.py``.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _naive(dt: datetime) -> datetime:
    """DuckDB TIMESTAMP columns have no timezone (see src/duckdb_conn.py) —
    normalize before comparing a DB-returned value against a tz-aware
    datetime we constructed locally. Tz-aware values (PG returns these)
    are converted to UTC first: the PG session may render timestamptz in
    the machine's local zone, and stripping tzinfo without converting
    made lease/expiry comparisons off by the UTC offset on any non-UTC
    machine."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo is not None else dt


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    # Route through `_open_duckdb` (rather than bare `duckdb.connect`) so
    # the session timezone is pinned to UTC — keeps `tests/db_pg/`'s
    # `test_no_bare_duckdb_connect_in_production_code` regression guard
    # green on new files.
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.jobs import JobsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return JobsRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    """Run migrations on the per-test PG engine, then return a PG repo."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.jobs_pg import JobsPgRepository

    return JobsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a ``jobs`` repo bound to either DuckDB or PG."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo


def _repo_from_resource(backend: str, resource: Any):
    if backend == "duckdb":
        from src.repositories.jobs import JobsRepository

        return JobsRepository(resource)
    from src.repositories.jobs_pg import JobsPgRepository

    return JobsPgRepository(resource)


@pytest.fixture(params=["duckdb", "pg"])
def repo_factory(request, tmp_path, pg_engine, monkeypatch):
    """Yields a zero-arg callable that builds a FRESH repo instance on
    every call, all sharing the same underlying connection (DuckDB) or
    engine (PG).

    This mirrors real usage: the production factory (``jobs_repo()``)
    constructs a brand-new repo object per call/request, wrapping the
    same underlying connection/engine — it never hands out a shared repo
    instance to concurrent callers. A test that reuses ONE repo instance
    across threads (like the plain ``repo`` fixture) would keep any
    per-instance state (e.g. a lock) alive and shared too, silently
    masking bugs where that state should have been module-level instead.
    Use this fixture for concurrency regression tests.
    """
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield lambda: _repo_from_resource("duckdb", conn)
        conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        import src.db_pg as db_pg

        engine = db_pg.get_engine()
        yield lambda: _repo_from_resource("pg", engine)


# ---------------------------------------------------------------------------
# contract tests — same calls, same answers from both engines
# ---------------------------------------------------------------------------


def test_enqueue_returns_queued_row(repo):
    row = repo.enqueue("send_email", {"to": "a@example.com"})
    assert row["id"]
    assert row["status"] == "queued"
    assert row["kind"] == "send_email"
    assert row["payload_json"] == {"to": "a@example.com"}
    assert row["priority"] == 0
    assert row["attempts"] == 0
    assert row["max_attempts"] == 3
    assert row["created_at"] is not None
    # "deduped" is response metadata (not a jobs column) — a fresh insert
    # always reports False. See JobsRepository.enqueue's docstring.
    assert row["deduped"] is False


def test_enqueue_defaults_payload_to_empty_dict(repo):
    row = repo.enqueue("noop", {})
    assert row["payload_json"] == {}


def test_get_roundtrip(repo):
    enqueued = repo.enqueue("send_email", {"to": "b@example.com"}, priority=5, max_attempts=7)
    fetched = repo.get(enqueued["id"])
    assert fetched is not None
    assert fetched["id"] == enqueued["id"]
    assert fetched["kind"] == "send_email"
    assert fetched["priority"] == 5
    assert fetched["max_attempts"] == 7
    assert fetched["payload_json"] == {"to": "b@example.com"}


def test_get_unknown_returns_none(repo):
    assert repo.get("does-not-exist") is None


def test_enqueue_respects_run_after(repo):
    run_after = datetime.now(timezone.utc) + timedelta(hours=1)
    row = repo.enqueue("scheduled_task", {}, run_after=run_after)
    fetched = repo.get(row["id"])
    assert fetched["run_after"] is not None


def test_idempotency_dedup_returns_same_job_while_queued(repo):
    first = repo.enqueue("send_email", {"to": "c@example.com"}, idempotency_key="dup-key-1")
    second = repo.enqueue("send_email", {"to": "different@example.com"}, idempotency_key="dup-key-1")
    assert second["id"] == first["id"]
    # the dedup hit returned the ORIGINAL row, not a re-insert with the
    # second call's payload
    assert second["payload_json"] == {"to": "c@example.com"}
    # only one row was actually created
    assert len(repo.list(kind="send_email")) == 1
    # "deduped" reports which call actually inserted vs. which call hit
    # the idempotency dedup — this is what POST /api/sync/trigger
    # branches on to decide 200 vs. 409 race-free (see app/api/sync.py).
    assert first["deduped"] is False
    assert second["deduped"] is True


def test_enqueue_deduped_false_for_distinct_keys(repo):
    """No collision -> both calls report a fresh insert, never `deduped`."""
    a = repo.enqueue("send_email", {}, idempotency_key="fresh-key-a")
    b = repo.enqueue("send_email", {}, idempotency_key="fresh-key-b")
    assert a["deduped"] is False
    assert b["deduped"] is False


def test_no_dedup_without_idempotency_key(repo):
    repo.enqueue("send_email", {"to": "d@example.com"})
    repo.enqueue("send_email", {"to": "d@example.com"})
    assert len(repo.list(kind="send_email")) == 2


def test_distinct_idempotency_keys_do_not_collide(repo):
    a = repo.enqueue("send_email", {}, idempotency_key="key-a")
    b = repo.enqueue("send_email", {}, idempotency_key="key-b")
    assert a["id"] != b["id"]


def test_list_filters_by_status(repo):
    repo.enqueue("a", {})
    repo.enqueue("b", {})
    all_jobs = repo.list()
    assert len(all_jobs) == 2
    queued = repo.list(status="queued")
    assert len(queued) == 2
    done = repo.list(status="done")
    assert done == []


def test_list_filters_by_kind(repo):
    repo.enqueue("alpha", {})
    repo.enqueue("beta", {})
    repo.enqueue("alpha", {})
    assert len(repo.list(kind="alpha")) == 2
    assert len(repo.list(kind="beta")) == 1
    assert repo.list(kind="gamma") == []


def test_list_respects_limit(repo):
    for i in range(5):
        repo.enqueue("bulk", {"i": i})
    assert len(repo.list(kind="bulk", limit=2)) == 2
    assert len(repo.list(kind="bulk", limit=50)) == 5


# ---------------------------------------------------------------------------
# claim / lease / complete / fail lifecycle
# ---------------------------------------------------------------------------


def test_lane_constants(repo):
    assert type(repo).HEAVY_LANE == "heavy"
    assert type(repo).LIGHT_LANE == "light"
    assert type(repo).EXTRACTION_LANE == "extraction"


def test_claim_next_returns_none_when_nothing_eligible(repo):
    assert repo.claim_next(kinds=["nope"], worker_id="w1") is None


def test_claim_next_returns_none_for_empty_kinds(repo):
    repo.enqueue("send_email", {})
    assert repo.claim_next(kinds=[], worker_id="w1") is None


def test_claim_next_filters_by_kind(repo):
    repo.enqueue("kind_a", {})
    b = repo.enqueue("kind_b", {})
    claimed = repo.claim_next(kinds=["kind_b"], worker_id="w1")
    assert claimed is not None
    assert claimed["id"] == b["id"]


def test_claim_next_sets_running_lease_and_increments_attempts(repo):
    job = repo.enqueue("send_email", {}, max_attempts=5)
    before = datetime.now(timezone.utc)
    claimed = repo.claim_next(kinds=["send_email"], worker_id="w1", lease_seconds=60)
    assert claimed is not None
    assert claimed["id"] == job["id"]
    assert claimed["status"] == "running"
    assert claimed["leased_by"] == "w1"
    assert claimed["lease_token"], "claim_next must mint a lease_token"
    assert claimed["attempts"] == 1
    assert claimed["started_at"] is not None
    assert claimed["lease_expires_at"] is not None
    assert _naive(claimed["lease_expires_at"]) > _naive(before)


def test_claim_next_skips_future_run_after(repo):
    run_after = datetime.now(timezone.utc) + timedelta(hours=1)
    repo.enqueue("scheduled", {}, run_after=run_after)
    assert repo.claim_next(kinds=["scheduled"], worker_id="w1") is None


def test_claim_next_claims_job_with_past_run_after(repo):
    run_after = datetime.now(timezone.utc) - timedelta(minutes=1)
    job = repo.enqueue("scheduled", {}, run_after=run_after)
    claimed = repo.claim_next(kinds=["scheduled"], worker_id="w1")
    assert claimed is not None
    assert claimed["id"] == job["id"]


def test_claim_next_orders_by_priority_then_fifo(repo):
    low = repo.enqueue("ordered", {}, priority=0)
    time.sleep(0.02)
    high1 = repo.enqueue("ordered", {}, priority=5)
    time.sleep(0.02)
    high2 = repo.enqueue("ordered", {}, priority=5)

    first = repo.claim_next(kinds=["ordered"], worker_id="w1")
    second = repo.claim_next(kinds=["ordered"], worker_id="w1")
    third = repo.claim_next(kinds=["ordered"], worker_id="w1")

    assert [first["id"], second["id"], third["id"]] == [high1["id"], high2["id"], low["id"]]


def test_claim_next_does_not_reclaim_before_lease_expires(repo):
    job = repo.enqueue("live_lease", {}, max_attempts=5)
    repo.claim_next(kinds=["live_lease"], worker_id="w1", lease_seconds=120)
    assert repo.claim_next(kinds=["live_lease"], worker_id="w2") is None
    # sanity: the job wasn't touched by the failed reclaim attempt
    assert repo.get(job["id"])["leased_by"] == "w1"


def test_claim_next_reclaims_expired_lease_and_preserves_started_at(repo):
    """Crash-recovery reclaim: a 'running' job whose lease has expired
    becomes claimable again, attempts increments again, but started_at
    (the original first-start time) is preserved rather than reset."""
    job = repo.enqueue("reclaimable", {}, max_attempts=5)
    # lease_seconds=-5 => lease_expires_at is already in the past
    first = repo.claim_next(kinds=["reclaimable"], worker_id="w1", lease_seconds=-5)
    assert first["attempts"] == 1
    started_at_1 = first["started_at"]

    second = repo.claim_next(kinds=["reclaimable"], worker_id="w2", lease_seconds=60)
    assert second is not None
    assert second["id"] == job["id"]
    assert second["leased_by"] == "w2"
    assert second["attempts"] == 2
    assert second["started_at"] == started_at_1
    assert second["lease_token"] != first["lease_token"], "a reclaim must mint a fresh lease_token"


def test_claim_next_does_not_reclaim_at_max_attempts(repo):
    job = repo.enqueue("capped", {}, max_attempts=1)
    first = repo.claim_next(kinds=["capped"], worker_id="w1", lease_seconds=-5)
    assert first["attempts"] == 1  # == max_attempts now
    assert repo.claim_next(kinds=["capped"], worker_id="w2") is None
    assert repo.get(job["id"])["leased_by"] == "w1"


def test_heartbeat_extends_lease_for_owner(repo):
    repo.enqueue("hb", {})
    claimed = repo.claim_next(kinds=["hb"], worker_id="w1", lease_seconds=1)
    ok = repo.heartbeat(claimed["id"], "w1", claimed["lease_token"], lease_seconds=9999)
    assert ok is True
    refreshed = repo.get(claimed["id"])
    assert refreshed["lease_expires_at"] > claimed["lease_expires_at"]


def test_heartbeat_false_for_wrong_token(repo):
    claimed_job = repo.enqueue("hb2", {})
    claimed = repo.claim_next(kinds=["hb2"], worker_id="w1")
    assert repo.heartbeat(claimed["id"], "w2", "not-the-real-token") is False
    # unaffected
    assert repo.get(claimed_job["id"])["leased_by"] == "w1"


def test_heartbeat_false_for_unknown_or_not_running_job(repo):
    assert repo.heartbeat("does-not-exist", "w1", "no-such-token") is False
    never_claimed = repo.enqueue("hb3", {})
    assert repo.heartbeat(never_claimed["id"], "anyone", "no-such-token") is False


def test_complete_marks_done_for_owner(repo):
    repo.enqueue("done_kind", {})
    claimed = repo.claim_next(kinds=["done_kind"], worker_id="w1")
    mutated = repo.complete(claimed["id"], "w1", claimed["lease_token"])
    assert mutated is True, "complete() must report the row was actually mutated"
    row = repo.get(claimed["id"])
    assert row["status"] == "done"
    assert row["finished_at"] is not None
    assert row["lease_expires_at"] is None


def test_complete_is_noop_for_wrong_token(repo):
    """MEDIUM 1: a stale-lease `complete()` call must report `False` (not
    just avoid raising) — callers (the worker runtime's webhook notify)
    gate side effects on this."""
    repo.enqueue("done_kind2", {})
    claimed = repo.claim_next(kinds=["done_kind2"], worker_id="w1")
    mutated = repo.complete(claimed["id"], "w1", "wrong-token")
    assert mutated is False
    row = repo.get(claimed["id"])
    assert row["status"] == "running"
    assert row["finished_at"] is None


def test_complete_with_result_merges_into_payload(repo):
    """Task 9: `complete(..., result=...)` merges the handler's return value
    into `payload_json["result"]` instead of requiring a schema column —
    see `JobsRepository.complete`'s docstring for why."""
    repo.enqueue("agent_response", {"prompt": "hi"})
    claimed = repo.claim_next(kinds=["agent_response"], worker_id="w1")
    repo.complete(claimed["id"], "w1", claimed["lease_token"], {"answer": "hello", "session_id": "s1"})
    row = repo.get(claimed["id"])
    assert row["status"] == "done"
    assert row["payload_json"]["prompt"] == "hi"  # original payload preserved
    assert row["payload_json"]["result"] == {"answer": "hello", "session_id": "s1"}


def test_complete_without_result_does_not_touch_payload(repo):
    """Every non-`agent_response` kind calls `complete()` with no `result`
    (the default) — payload_json must stay byte-identical."""
    repo.enqueue("done_kind3", {"x": 1})
    claimed = repo.claim_next(kinds=["done_kind3"], worker_id="w1")
    repo.complete(claimed["id"], "w1", claimed["lease_token"])
    row = repo.get(claimed["id"])
    assert row["payload_json"] == {"x": 1}


def test_complete_with_result_is_noop_for_wrong_token(repo):
    repo.enqueue("agent_response", {"prompt": "hi"})
    claimed = repo.claim_next(kinds=["agent_response"], worker_id="w1")
    repo.complete(claimed["id"], "w1", "wrong-token", {"answer": "should not land"})
    row = repo.get(claimed["id"])
    assert row["status"] == "running"
    assert "result" not in row["payload_json"]


def test_fail_with_retry_requeues(repo):
    """A successful requeue is NOT terminal — `fail()` must report
    `False` even though it mutated the row (to `'queued'`, not
    `'failed'`), so callers don't fire a premature `job.failed` webhook."""
    repo.enqueue("retry_kind", {}, max_attempts=5)
    claimed = repo.claim_next(kinds=["retry_kind"], worker_id="w1")
    assert claimed["attempts"] == 1
    finalized = repo.fail(claimed["id"], "w1", claimed["lease_token"], "boom", retry_in_seconds=30)
    assert finalized is False
    row = repo.get(claimed["id"])
    assert row["status"] == "queued"
    assert row["run_after"] is not None
    assert row["error"] == "boom"
    assert row["lease_expires_at"] is None
    assert row["leased_by"] is None
    assert row["lease_token"] is None


def test_fail_at_max_attempts_finalizes(repo):
    repo.enqueue("fail_kind", {}, max_attempts=1)
    claimed = repo.claim_next(kinds=["fail_kind"], worker_id="w1")
    assert claimed["attempts"] == 1
    finalized = repo.fail(claimed["id"], "w1", claimed["lease_token"], "boom", retry_in_seconds=30)
    assert finalized is True, "fail() must report the row was finalized to 'failed'"
    row = repo.get(claimed["id"])
    assert row["status"] == "failed"
    assert row["finished_at"] is not None
    assert row["error"] == "boom"


def test_fail_without_retry_in_seconds_finalizes_even_with_attempts_remaining(repo):
    repo.enqueue("no_retry_kind", {}, max_attempts=5)
    claimed = repo.claim_next(kinds=["no_retry_kind"], worker_id="w1")
    finalized = repo.fail(claimed["id"], "w1", claimed["lease_token"], "boom")
    assert finalized is True
    row = repo.get(claimed["id"])
    assert row["status"] == "failed"


def test_fail_is_noop_for_wrong_token(repo):
    """MEDIUM 1: a stale-lease `fail()` call must report `False`."""
    repo.enqueue("fail_kind2", {}, max_attempts=5)
    claimed = repo.claim_next(kinds=["fail_kind2"], worker_id="w1")
    finalized = repo.fail(claimed["id"], "w1", "wrong-token", "boom", retry_in_seconds=30)
    assert finalized is False
    row = repo.get(claimed["id"])
    assert row["status"] == "running"
    assert row["error"] is None


# ---------------------------------------------------------------------------
# reap_exhausted — stuck-job reaper (wave-2B worker-runtime carry-over)
# ---------------------------------------------------------------------------


def test_reap_exhausted_finalizes_stuck_running_job_at_max_attempts(repo):
    """A job whose lease expired on its LAST attempt is not reclaimable by
    ``claim_next()`` (attempts >= max_attempts) and would stay 'running'
    forever without the reaper.

    ``reap_exhausted()`` returns the reaped job ROWS (not just a count) —
    see its docstring: the worker's reap sweep needs `id`/`kind`/
    `payload_json` to fire the same terminal webhook notify a live
    ``fail()`` call would have.
    """
    job = repo.enqueue("stuck", {"agent_id": "agent-1"}, max_attempts=1)
    claimed = repo.claim_next(kinds=["stuck"], worker_id="w1", lease_seconds=-5)
    assert claimed["attempts"] == 1  # == max_attempts now
    # confirm it's genuinely stuck: not reclaimable
    assert repo.claim_next(kinds=["stuck"], worker_id="w2") is None

    reaped = repo.reap_exhausted()
    assert len(reaped) == 1
    assert reaped[0]["id"] == job["id"]
    assert reaped[0]["kind"] == "stuck"
    assert reaped[0]["payload_json"] == {"agent_id": "agent-1"}
    row = repo.get(job["id"])
    assert row["status"] == "failed"
    assert row["finished_at"] is not None
    assert row["lease_expires_at"] is None
    assert row["error"] == "lease expired after max attempts"


def test_reap_exhausted_ignores_running_job_with_attempts_remaining(repo):
    job = repo.enqueue("not_stuck", {}, max_attempts=5)
    repo.claim_next(kinds=["not_stuck"], worker_id="w1", lease_seconds=-5)
    assert repo.reap_exhausted() == []
    assert repo.get(job["id"])["status"] == "running"


def test_reap_exhausted_ignores_running_job_with_live_lease(repo):
    job = repo.enqueue("live_stuck", {}, max_attempts=1)
    repo.claim_next(kinds=["live_stuck"], worker_id="w1", lease_seconds=120)
    assert repo.reap_exhausted() == []
    assert repo.get(job["id"])["status"] == "running"


def test_reap_exhausted_ignores_queued_and_done_jobs(repo):
    repo.enqueue("queued_kind", {})
    done_job = repo.enqueue("done_kind3", {})
    claimed = repo.claim_next(kinds=["done_kind3"], worker_id="w1")
    repo.complete(claimed["id"], "w1", claimed["lease_token"])
    assert repo.reap_exhausted() == []
    assert repo.get(done_job["id"])["status"] == "done"


def test_reap_exhausted_returns_rows_across_multiple_stuck_jobs(repo):
    job_a = repo.enqueue("stuck_bulk", {}, max_attempts=1)
    job_b = repo.enqueue("stuck_bulk", {}, max_attempts=1)
    repo.claim_next(kinds=["stuck_bulk"], worker_id="w1", lease_seconds=-5)
    repo.claim_next(kinds=["stuck_bulk"], worker_id="w1", lease_seconds=-5)
    reaped = repo.reap_exhausted()
    assert {r["id"] for r in reaped} == {job_a["id"], job_b["id"]}
    assert repo.get(job_a["id"])["status"] == "failed"
    assert repo.get(job_b["id"])["status"] == "failed"


# ---------------------------------------------------------------------------
# reclaim race edges — permanent regression tests for the previous-review
# carry-over findings (heartbeat-vs-reclaim, complete-vs-reclaim). Both are
# simulated as deterministic single-thread interleavings (direct repo calls
# in the racy order) rather than real concurrent threads — the guard each
# no-op relies on (``WHERE leased_by = ? AND status = 'running'``) is
# already exercised concurrently by test_concurrent_claim_never_double_claims
# above; what these lock down is the *sequence*: stale-owner action AFTER a
# reclaim must never affect the new owner's state.
# ---------------------------------------------------------------------------


def test_heartbeat_after_reclaim_never_extends_new_owners_lease(repo):
    job = repo.enqueue("hb_vs_reclaim", {}, max_attempts=5)
    # w1 claims with an already-expired lease (simulates a crashed/stalled
    # worker whose lease ran out) ...
    stale = repo.claim_next(kinds=["hb_vs_reclaim"], worker_id="w1", lease_seconds=-5)
    assert stale is not None
    # ... w2 reclaims the same job before w1 sends its next heartbeat.
    reclaimed = repo.claim_next(kinds=["hb_vs_reclaim"], worker_id="w2", lease_seconds=120)
    assert reclaimed is not None
    assert reclaimed["leased_by"] == "w2"
    lease_before = reclaimed["lease_expires_at"]

    # w1, unaware it was reclaimed, sends a stale heartbeat using its OLD token.
    ok = repo.heartbeat(job["id"], "w1", stale["lease_token"], lease_seconds=9999)
    assert ok is False

    row = repo.get(job["id"])
    assert row["leased_by"] == "w2"
    assert row["status"] == "running"
    assert row["lease_expires_at"] == lease_before  # w2's lease untouched by w1's stale heartbeat


def test_complete_after_reclaim_is_noop_and_reclaim_never_claims_done_job(repo):
    job = repo.enqueue("complete_vs_reclaim", {}, max_attempts=5)
    stale = repo.claim_next(kinds=["complete_vs_reclaim"], worker_id="w1", lease_seconds=-5)
    assert stale is not None
    reclaimed = repo.claim_next(kinds=["complete_vs_reclaim"], worker_id="w2", lease_seconds=120)
    assert reclaimed is not None

    # w1 finishes its (already reclaimed) work late and calls complete() with its OLD token.
    repo.complete(job["id"], "w1", stale["lease_token"])
    row = repo.get(job["id"])
    assert row["status"] == "running"  # unaffected — w1 is no longer the owner
    assert row["leased_by"] == "w2"
    assert row["finished_at"] is None

    # the legitimate owner (w2) completing it with the CURRENT token IS honored.
    repo.complete(job["id"], "w2", reclaimed["lease_token"])
    row2 = repo.get(job["id"])
    assert row2["status"] == "done"
    assert row2["finished_at"] is not None

    # a 'done' job is never claimable again — not queued, and not a
    # reclaimable 'running' job with an expired lease.
    assert repo.claim_next(kinds=["complete_vs_reclaim"], worker_id="w3") is None


# ---------------------------------------------------------------------------
# same-worker reclaim — Critical: all lane slots in ONE worker process share
# a single worker_id (hostname:pid). A worker_id-only guard cannot tell a
# stale slot's late call apart from a same-process reclaim of the same job
# by a DIFFERENT slot — a same-worker double-execution bug (empirically
# reproduced, `fail()` even led to a third concurrent execution via the
# requeue it issued). lease_token — minted fresh on every claim, including a
# same-worker reclaim — is the guard these tests lock down.
# ---------------------------------------------------------------------------


def test_same_worker_reclaim_mints_a_fresh_lease_token(repo):
    job = repo.enqueue("self_reclaim_token", {}, max_attempts=5)
    stale = repo.claim_next(kinds=["self_reclaim_token"], worker_id="shared-worker", lease_seconds=-5)
    assert stale is not None
    reclaimed = repo.claim_next(kinds=["self_reclaim_token"], worker_id="shared-worker", lease_seconds=120)
    assert reclaimed is not None
    assert reclaimed["leased_by"] == stale["leased_by"] == "shared-worker"
    assert reclaimed["lease_token"] != stale["lease_token"]
    assert repo.get(job["id"])["lease_token"] == reclaimed["lease_token"]


def test_same_worker_stale_complete_after_same_worker_reclaim_is_noop(repo):
    job = repo.enqueue("self_reclaim_complete", {}, max_attempts=5)
    stale = repo.claim_next(kinds=["self_reclaim_complete"], worker_id="shared-worker", lease_seconds=-5)
    reclaimed = repo.claim_next(kinds=["self_reclaim_complete"], worker_id="shared-worker", lease_seconds=120)
    assert reclaimed["lease_token"] != stale["lease_token"]

    # The stale slot, unaware it was reclaimed by ANOTHER slot of the same
    # process (same worker_id!), finishes late and calls complete() with
    # its OLD token.
    repo.complete(job["id"], "shared-worker", stale["lease_token"])
    row = repo.get(job["id"])
    assert row["status"] == "running", "stale same-worker complete() must not clobber the live (reclaimed) claim"
    assert row["lease_token"] == reclaimed["lease_token"]

    # the legitimate (reclaiming) slot completing it with the CURRENT token IS honored.
    repo.complete(job["id"], "shared-worker", reclaimed["lease_token"])
    row2 = repo.get(job["id"])
    assert row2["status"] == "done"


def test_same_worker_stale_fail_after_same_worker_reclaim_is_noop(repo):
    job = repo.enqueue("self_reclaim_fail", {}, max_attempts=5)
    stale = repo.claim_next(kinds=["self_reclaim_fail"], worker_id="shared-worker", lease_seconds=-5)
    reclaimed = repo.claim_next(kinds=["self_reclaim_fail"], worker_id="shared-worker", lease_seconds=120)
    assert reclaimed["lease_token"] != stale["lease_token"]

    # The stale slot's handler raises late and calls fail() (with a retry)
    # using its OLD token — must not requeue the live claim out from under
    # the reclaiming slot (this was the path that led to a THIRD concurrent
    # execution in the empirical repro: the stale fail() requeued the job,
    # a third slot claimed it, while the reclaiming slot's handler was
    # still running).
    repo.fail(job["id"], "shared-worker", stale["lease_token"], "stale failure", retry_in_seconds=30)
    row = repo.get(job["id"])
    assert row["status"] == "running", "stale same-worker fail() must not requeue/clobber the live claim"
    assert row["error"] is None
    assert row["lease_token"] == reclaimed["lease_token"]

    # the legitimate (reclaiming) slot's fail() with the CURRENT token IS honored.
    repo.fail(job["id"], "shared-worker", reclaimed["lease_token"], "real failure", retry_in_seconds=30)
    row2 = repo.get(job["id"])
    assert row2["status"] == "queued"
    assert row2["error"] == "real failure"


def test_same_worker_heartbeat_with_old_token_returns_false_after_reclaim(repo):
    job = repo.enqueue("self_reclaim_hb", {}, max_attempts=5)
    stale = repo.claim_next(kinds=["self_reclaim_hb"], worker_id="shared-worker", lease_seconds=-5)
    reclaimed = repo.claim_next(kinds=["self_reclaim_hb"], worker_id="shared-worker", lease_seconds=120)
    assert reclaimed["lease_token"] != stale["lease_token"]
    lease_before = reclaimed["lease_expires_at"]

    ok = repo.heartbeat(job["id"], "shared-worker", stale["lease_token"], lease_seconds=9999)
    assert ok is False

    row = repo.get(job["id"])
    assert row["leased_by"] == "shared-worker"  # same worker_id both times — token is what differs
    assert row["lease_token"] == reclaimed["lease_token"]
    assert row["lease_expires_at"] == lease_before  # untouched by the stale heartbeat


def test_concurrent_claim_never_double_claims_two_jobs_two_threads(repo_factory):
    """Two threads race to claim from a pool of two queued jobs. Neither
    may claim the same row as the other (no double-claim); it's fine if
    a thread comes away with ``None`` (no eligible job left), but with
    exactly two distinct jobs and two threads that shouldn't happen
    absent a bug. Each thread builds its OWN repo instance via
    ``repo_factory`` — mirrors the production factory building a fresh
    repo per caller — same rationale as the enqueue race test above.
    """
    job_a = repo_factory().enqueue("race_claim", {"i": 0})
    job_b = repo_factory().enqueue("race_claim", {"i": 1})

    n = 2
    barrier = threading.Barrier(n)
    results: list[dict | None] = [None, None]
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=5)
            results[i] = repo_factory().claim_next(kinds=["race_claim"], worker_id=f"w{i}")
        except BaseException as exc:  # noqa: BLE001 - surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"claim_next raised under concurrency: {errors}"
    claimed = [r for r in results if r is not None]
    claimed_ids = [r["id"] for r in claimed]
    assert len(claimed_ids) == len(set(claimed_ids)), "same job claimed by both threads"
    # with exactly two distinct eligible jobs and two threads, both should
    # succeed and claim one each
    assert sorted(claimed_ids) == sorted([job_a["id"], job_b["id"]])


def test_concurrent_enqueue_same_key_dedups_to_exactly_one_row(repo_factory):
    """Regression test for the PG dedup race: 8 threads enqueue the same
    ``idempotency_key`` concurrently. Under a plain SELECT-then-INSERT on
    Postgres (READ COMMITTED), concurrent transactions can each miss the
    others' uncommitted row and all insert — empirically confirmed to
    produce 8 rows. Exactly one row must exist afterward, on both
    backends (the DuckDB path exercises the module-level enqueue lock
    instead of a cross-transaction race).

    Each thread builds its OWN repo instance via ``repo_factory`` (as the
    production ``jobs_repo()`` factory does per caller) rather than
    sharing one repo object — this is what caught the DuckDB bug where
    the dedup lock lived on ``self`` instead of at module scope: with a
    shared instance the shared lock papered over the race; with separate
    instances (all wrapping the same underlying connection) it did not.
    """
    n = 8
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []
    results: list[dict] = []
    results_lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=5)
            row = repo_factory().enqueue("send_email", {"i": i}, idempotency_key="race-key")
            with results_lock:
                results.append(row)
        except BaseException as exc:  # noqa: BLE001 - surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"enqueue raised under concurrency: {errors}"
    matching = repo_factory().list(kind="send_email", limit=50)
    assert len(matching) == 1

    # Exactly one of the 8 concurrent callers actually inserted the row
    # (`deduped=False`); the other 7 all collapsed onto it
    # (`deduped=True`). This is the same signal `POST /api/sync/trigger`
    # uses to decide 200 vs. 409 without a racy pre-check.
    assert len(results) == n
    deduped_flags = [r["deduped"] for r in results]
    assert deduped_flags.count(False) == 1
    assert deduped_flags.count(True) == n - 1


# ---------------------------------------------------------------------------
# PG-only: fallback-miss retry
# ---------------------------------------------------------------------------


def test_pg_enqueue_retries_insert_when_fallback_select_misses(pg_engine, monkeypatch):
    """Regression test for the PG fallback-miss race.

    Sequence: our INSERT loses the ``ON CONFLICT`` race (another job
    already holds the key as 'queued'/'running') -> before our fallback
    SELECT runs, the winning row's status flips to 'done' (job finished)
    -> the fallback SELECT (``WHERE status IN ('queued', 'running')``)
    finds nothing. The key is legitimately free for reuse at that point
    (the partial unique index no longer excludes it), so ``enqueue()``
    must retry the INSERT and succeed, rather than asserting/crashing on
    ``row is None``.

    Simulated via a SQLAlchemy ``after_cursor_execute`` event: right after
    our own ``INSERT ... ON CONFLICT ... DO NOTHING`` executes and misses
    (finds the key still held by 'winner'), we flip the winner's row to
    'done' out-of-band — a separate connection, committed immediately —
    before ``enqueue()``'s code moves on to run the fallback SELECT. Under
    READ COMMITTED that SELECT then deterministically observes the
    post-flip state and misses too, forcing the retry path.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    engine = db_pg.get_engine()

    from src.repositories.jobs_pg import JobsPgRepository

    repo = JobsPgRepository(engine)

    # Seed the "winner": a queued job holding the idempotency key.
    winner = repo.enqueue("send_email", {"who": "winner"}, idempotency_key="flip-key")

    import sqlalchemy as sa

    flipped = {"done": False}

    def _flip_winner_to_done(conn, cursor, statement, parameters, context, executemany):
        # Fires right after OUR INSERT ... ON CONFLICT ... DO NOTHING
        # executes and misses (the key is still held by 'winner'). Flip
        # 'winner' to 'done' on a separate connection, committed
        # immediately, before enqueue()'s code moves on to the fallback
        # SELECT — so that SELECT deterministically misses too. Guarded
        # to fire only once so the retried INSERT doesn't re-trigger it.
        if not flipped["done"] and "INSERT INTO jobs" in statement and "ON CONFLICT" in statement:
            flipped["done"] = True
            with engine.connect() as side_conn:
                side_conn.execute(
                    sa.text("UPDATE jobs SET status = 'done' WHERE id = :id"),
                    {"id": winner["id"]},
                )
                side_conn.commit()

    sa.event.listen(engine, "after_cursor_execute", _flip_winner_to_done)
    try:
        # This enqueue call loses the initial INSERT race (key still held
        # by 'winner' at INSERT time), then the event hook flips 'winner'
        # to 'done' right after — before the fallback SELECT runs — so
        # that SELECT must miss too, forcing a retry INSERT that should
        # succeed.
        second = repo.enqueue("send_email", {"who": "second"}, idempotency_key="flip-key")
    finally:
        sa.event.remove(engine, "after_cursor_execute", _flip_winner_to_done)

    assert flipped["done"], "test setup bug: the fallback SELECT hook never fired"
    assert second["id"] != winner["id"]
    assert second["status"] == "queued"
    assert second["payload_json"] == {"who": "second"}
    # The retry succeeded as a fresh INSERT (not a dedup hit) — even
    # though this call initially lost the ON CONFLICT race, it ends up
    # being the one that actually created a row.
    assert second["deduped"] is False

    # Both rows now exist: the original (now done) and the new retried insert.
    all_matching = repo.list(kind="send_email", limit=50)
    ids = {r["id"] for r in all_matching}
    assert {winner["id"], second["id"]} <= ids
