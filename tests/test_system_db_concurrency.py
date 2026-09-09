"""What actually serializes access to `system.duckdb` (#2352).

`POST /api/sync/trigger` was reported to make an instance unresponsive to
*every* endpoint for the whole sync window. The event-loop explanation is
already ruled out (the trigger only enqueues, and `dispatch_job` runs the
handler under `asyncio.to_thread`), which left one open question: since
`get_system_db()` hands out a `.cursor()` off ONE process-wide DuckDB
connection, **does DuckDB's Python driver serialize statement execution
across cursors of the same base connection?** If it did, a multi-minute sync
doing frequent short `sync_state` writes would queue every auth/session
lookup behind it.

**Measured answer: no — DuckDB does not serialize across sibling cursors.**
Raw numbers from the experiment these tests were distilled from (4 reader
threads on `get_system_db()`, DuckDB 1.5.5, DATA_DIR on local disk):

| writer shape (3 s window)              | reader p50 | reader p95 | reader max | reads done |
|----------------------------------------|-----------:|-----------:|-----------:|-----------:|
| none (baseline)                        |    1.60 ms |    2.89 ms |    8.50 ms |      7 862 |
| 553 short `sync_state` upserts         |    2.12 ms |    5.28 ms |  122.85 ms |      5 046 |
| ONE 6.8 s `CREATE TABLE AS` statement  |    1.79 ms |    4.27 ms |   25.82 ms |     13 619 |
| write txn held open for the whole 3 s  |    1.68 ms |    4.06 ms |    9.61 ms |      6 815 |

Neither the sync-shaped write stream nor a single long statement nor a held
write transaction blocks readers. That refutes the DuckDB-contention theory.

**What DOES serialize is Agnes's own `_system_db_lock`.** `get_system_db()`
takes it on *every* call, and `checkpoint_system_db()` /
`checkpoint_operational_db()` used to hold that same lock across
`execute("CHECKPOINT")` — a statement `app/main.py`'s own checkpoint-loop
docstring notes "can block while DuckDB flushes a large WAL". Measured with a
942 MB WAL, again 4 reader threads:

| `checkpoint_system_db()`         | statement took | reads completed during it | reader p50 |
|---------------------------------|---------------:|--------------------------:|-----------:|
| executing UNDER the lock (before)|        7.14 s |     4 (one per thread)    |  7 137 ms  |
| cursor taken under, run outside  |       10.52 s |                    17 589 |    1.88 ms |

Under the lock, every reader parked inside `get_system_db()` for the whole
flush — a 4 700x latency blow-up and exactly the reported symptom. The WAL
folds to zero either way, so the durability the tick exists for is unchanged
(pinned below).

Absolute statement durations vary with disk and load (these were taken on a
busy multi-core box, hence the spread), which is why the tests below assert
the *mechanism* — a reader completes while a CHECKPOINT is mid-execution,
gated on an `Event` rather than a millisecond threshold — and stay
deterministic on a loaded runner.

`refresh_rolling_snapshot` already takes its cursor under the lock and runs
`EXPORT DATABASE` outside it, for this exact reason (#1294); the two CHECKPOINT
accessors were left behind.

**The other half of that discipline** (#1294's follow-up, third class below):
a statement that runs on a *child cursor* outside the lock can be abandoned
mid-flight — `to_thread_drain_on_cancel` hands `CancelledError` to the awaiter
once the shared shutdown drain budget is spent while the OS thread keeps going
— and the lifespan then closes the parent connection out from under it. Moving
the CHECKPOINT off the lock therefore brings it under the same contract as the
export: publish the cursor, and let every close path interrupt + bounded-wait
on it. Reachability is not hypothetical, it is the same loop
(`app/main.py::_state_checkpoint_loop`, the FIRST task the lifespan cancels)
and a 7 s statement against a 10 s default budget.
"""

from __future__ import annotations

import statistics
import threading
import time

import pytest

import src.db as dbmod
from src.db import checkpoint_operational_db, checkpoint_system_db, get_operational_db, get_system_db

# Generous by design: these bound a hang, they do not measure latency. A
# reader that has not returned in this long is parked on a lock, not slow.
_JOIN_TIMEOUT_S = 30.0

_READ_SQL = "SELECT id FROM users WHERE email = ?"
_EMAIL = "concurrency@example.com"


@pytest.fixture
def system_db(tmp_path, monkeypatch):
    """A DuckDB app-state `system.duckdb` with one user row to read back.

    `get_system_db()` raises on a Postgres instance by design (see its
    docstring), so this experiment pins the DuckDB backend explicitly.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    conn = get_system_db()
    conn.execute(
        "INSERT INTO users (id, email, name) VALUES (?, ?, ?)",
        ["u-concurrency", _EMAIL, "Concurrency"],
    )
    conn.close()
    yield tmp_path
    dbmod.close_system_db()
    dbmod.close_operational_db()


def _read_once() -> str | None:
    cur = get_system_db()
    try:
        row = cur.execute(_READ_SQL, [_EMAIL]).fetchone()
    finally:
        cur.close()
    return row[0] if row else None


class _CheckpointGate:
    """Wraps the singleton connection so `CHECKPOINT` parks until released.

    Stands in for "DuckDB is flushing a large WAL" without needing a large
    WAL, which is what makes the assertion deterministic: the statement is
    in-flight for exactly as long as the test says it is.
    """

    def __init__(self, real, entered: threading.Event, release: threading.Event):
        self._real = real
        self._entered = entered
        self._release = release

    def cursor(self):
        return _CheckpointGate(self._real.cursor(), self._entered, self._release)

    def execute(self, sql, *args, **kwargs):
        if "CHECKPOINT" in str(sql).upper():
            self._entered.set()
            if not self._release.wait(timeout=_JOIN_TIMEOUT_S):  # pragma: no cover - safety
                raise AssertionError("gate never released")
        return self._real.execute(sql, *args, **kwargs)

    def close(self):
        # Never close the real singleton out from under the process.
        return None

    def __getattr__(self, name):
        return getattr(self._real, name)


def _assert_reader_unblocked_during_checkpoint(monkeypatch, conn_attr: str, checkpoint_fn, probe) -> None:
    """The mechanism assertion shared by both CHECKPOINT accessors.

    Parks the accessor inside `execute("CHECKPOINT")`, then requires an
    independent thread to get through the singleton accessor while that
    statement is still in flight. If the accessor holds `_system_db_lock`
    across execution, the probe never returns and the join times out.
    """
    entered = threading.Event()
    release = threading.Event()
    real = getattr(dbmod, conn_attr)
    assert real is not None, f"{conn_attr} singleton is not open"
    monkeypatch.setattr(dbmod, conn_attr, _CheckpointGate(real, entered, release))

    result: dict[str, object] = {}

    def run_checkpoint():
        try:
            result["checkpoint"] = checkpoint_fn()
        except BaseException as exc:  # pragma: no cover - surfaced via assertions
            result["checkpoint_error"] = exc

    def run_probe():
        started = time.perf_counter()
        try:
            result["probe"] = probe()
        except BaseException as exc:  # pragma: no cover - surfaced via assertions
            result["probe_error"] = exc
        result["probe_s"] = time.perf_counter() - started

    cp = threading.Thread(target=run_checkpoint, name="checkpoint")
    pr = threading.Thread(target=run_probe, name="probe")
    cp.start()
    try:
        assert entered.wait(timeout=_JOIN_TIMEOUT_S), "CHECKPOINT never started executing"
        pr.start()
        pr.join(timeout=_JOIN_TIMEOUT_S)
        still_parked = not release.is_set()
        blocked = pr.is_alive()
    finally:
        release.set()
        cp.join(timeout=_JOIN_TIMEOUT_S)
        # A still-parked probe unblocks the moment the gate opens; join it
        # before the fixture closes the singleton under it.
        if pr.is_alive():
            pr.join(timeout=_JOIN_TIMEOUT_S)

    assert still_parked, "test bug: the gate was released before the probe ran"
    assert not blocked, (
        f"{checkpoint_fn.__name__} held _system_db_lock across execute('CHECKPOINT'): the "
        f"singleton accessor was still blocked {_JOIN_TIMEOUT_S:.0f}s into the statement. "
        "Take the cursor under the lock and execute outside it, as "
        "refresh_rolling_snapshot does."
    )
    assert "probe_error" not in result, result.get("probe_error")
    assert "checkpoint_error" not in result, result.get("checkpoint_error")
    assert result["checkpoint"] is True


class TestDuckDBDoesNotSerializeAcrossSiblingCursors:
    """The open question in #2352 — measured, and refuted."""

    def test_an_open_write_transaction_does_not_block_reads_on_sibling_cursors(self, system_db):
        """The issue's "sync holds a write transaction open" theory.

        A write transaction is opened and left uncommitted; four readers then
        go through `get_system_db()`. All four must return while it is still
        open.
        """
        writing = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        answers: list[str | None] = []
        lock = threading.Lock()

        def reader():
            try:
                assert writing.wait(timeout=_JOIN_TIMEOUT_S)
                value = _read_once()
                with lock:
                    answers.append(value)
            except BaseException as exc:  # pragma: no cover - surfaced via assertions
                with lock:
                    errors.append(exc)

        writer = get_system_db()
        threads = [threading.Thread(target=reader, name=f"reader-{i}") for i in range(4)]
        try:
            writer.execute("BEGIN TRANSACTION")
            writer.execute("INSERT INTO sync_state (table_id, rows, status) VALUES ('held-txn', 1, 'ok')")
            for t in threads:
                t.start()
            writing.set()
            for t in threads:
                t.join(timeout=_JOIN_TIMEOUT_S)
            blocked = [t.name for t in threads if t.is_alive()]
        finally:
            release.set()
            writer.execute("ROLLBACK")
            writer.close()

        assert not errors, errors
        assert not blocked, (
            f"readers {blocked} never returned while one cursor held an open write "
            "transaction — DuckDB would be serializing sibling cursors"
        )
        assert answers == ["u-concurrency"] * 4

    def test_sustained_short_writes_do_not_serialize_reads(self, system_db, capsys):
        """The shape a sync actually has: many short `sync_state` upserts.

        Not a latency assertion — it asserts that readers keep completing
        *while* the write stream is in flight, many more times than the
        reader-thread count, which strict serialization could not produce.
        The measured latencies are printed for the record.
        """
        writes = 300
        readers = 2
        writer_running = threading.Event()
        writer_done = threading.Event()
        latencies: dict[str, list[float]] = {}
        errors: list[BaseException] = []

        def reader(key: str):
            observed: list[float] = []
            try:
                assert writer_running.wait(timeout=_JOIN_TIMEOUT_S)
                while not writer_done.is_set():
                    started = time.perf_counter()
                    _read_once()
                    observed.append(time.perf_counter() - started)
            except BaseException as exc:  # pragma: no cover - surfaced via assertions
                errors.append(exc)
            latencies[key] = observed

        threads = [threading.Thread(target=reader, args=(f"r{i}",)) for i in range(readers)]
        for t in threads:
            t.start()
        cur = get_system_db()
        write_latencies: list[float] = []
        try:
            writer_running.set()
            for n in range(writes):
                started = time.perf_counter()
                cur.execute(
                    "INSERT INTO sync_state (table_id, last_sync, rows, status) "
                    "VALUES (?, current_timestamp, ?, 'ok') "
                    "ON CONFLICT (table_id) DO UPDATE SET rows = excluded.rows, "
                    "last_sync = excluded.last_sync",
                    [f"sync-shape-{n % 40}", n],
                )
                write_latencies.append(time.perf_counter() - started)
        finally:
            writer_done.set()
            cur.close()
            for t in threads:
                t.join(timeout=_JOIN_TIMEOUT_S)

        assert not errors, errors
        assert all(not t.is_alive() for t in threads)
        reads = sum(len(v) for v in latencies.values())
        with capsys.disabled():
            print(
                f"\n[#2352] {writes} sync_state upserts (mean {statistics.mean(write_latencies) * 1000:.2f}ms) "
                f"vs {reads} concurrent reads through get_system_db() "
                f"(mean {statistics.mean([x for v in latencies.values() for x in v]) * 1000:.2f}ms)"
            )
        assert reads > 4 * readers, (
            f"only {reads} reads completed across {readers} reader threads during "
            f"{writes} concurrent short writes — reads are queueing behind writes"
        )


class TestCheckpointDoesNotHoldTheSingletonLock:
    """The mechanism that DOES block — and the contract that keeps it fixed."""

    def test_checkpoint_system_db_does_not_block_get_system_db(self, system_db, monkeypatch):
        _assert_reader_unblocked_during_checkpoint(monkeypatch, "_system_db_conn", checkpoint_system_db, _read_once)

    def test_checkpoint_operational_db_does_not_block_get_operational_db(self, system_db, monkeypatch):
        """`checkpoint_operational_db()` shares `_system_db_lock`.

        Worth its own case: on a Postgres app-state instance
        `operational.duckdb` is the only written DuckDB file, so this is the
        only arm of the checkpoint loop that runs there — and it gates CLI
        login and Slack identity binding.
        """
        get_operational_db().close()

        def probe():
            cur = get_operational_db()
            try:
                cur.execute("SELECT count(*) FROM cli_auth_codes").fetchone()
            finally:
                cur.close()
            return "ok"

        _assert_reader_unblocked_during_checkpoint(
            monkeypatch, "_operational_db_conn", checkpoint_operational_db, probe
        )

    def test_checkpoint_system_db_still_folds_the_wal(self, system_db):
        """The lock change must not cost the durability the loop exists for.

        A cursor is a sibling connection, so this pins that `CHECKPOINT`
        issued on one still flushes the shared WAL into the main file — the
        whole point of the periodic tick (#710).
        """
        wal = system_db / "state" / "system.duckdb.wal"
        cur = get_system_db()
        try:
            for n in range(200):
                cur.execute(
                    "INSERT INTO audit_log (id, user_id, action, resource) VALUES (?, 'u1', 'test.action', ?)",
                    [f"wal-{n}", str(n)],
                )
        finally:
            cur.close()
        assert wal.exists() and wal.stat().st_size > 0, "expected an unflushed WAL to checkpoint"

        assert checkpoint_system_db() is True
        assert not wal.exists() or wal.stat().st_size == 0, "CHECKPOINT did not fold the WAL"


class _BlockedCheckpointHarness:
    """A fake singleton whose cursor's CHECKPOINT blocks until `interrupt()`.

    Same shape as `tests/test_rolling_snapshot.py::_BlockedExportHarness`, for
    the same reason: it stands in for a real multi-second WAL flush in flight
    at the moment a lifecycle event must interrupt it rather than close the
    parent connection out from under it. Statements on the PARENT are
    pass-throughs — a close path's own final CHECKPOINT must not deadlock
    against the child it just interrupted.
    """

    def __init__(self, real_conn):
        self.checkpoint_started = threading.Event()
        self.interrupted = threading.Event()
        self.parent_closed = threading.Event()
        harness = self

        class _Cursor:
            def __init__(self, inner):
                self._c = inner

            def execute(self, sql, *a, **kw):
                if "CHECKPOINT" in str(sql).upper():
                    harness.checkpoint_started.set()
                    harness.interrupted.wait(timeout=_JOIN_TIMEOUT_S)
                    raise RuntimeError("interrupted")
                return self._c.execute(sql, *a, **kw)

            def interrupt(self):
                harness.interrupted.set()

            def close(self):
                return None

            def __getattr__(self, name):
                return getattr(self._c, name)

        class _Conn:
            def cursor(self):
                return _Cursor(real_conn.cursor())

            def execute(self, sql, *a, **kw):
                return None

            def close(self):
                harness.parent_closed.set()

        self.conn = _Conn()


def _drive_close_against_an_in_flight_checkpoint(monkeypatch, conn_attr, checkpoint_fn, close_fn):
    """Run `checkpoint_fn` until it is mid-CHECKPOINT, then `close_fn`.

    Asserts the close path interrupted the child cursor and did not spend its
    whole bounded wait — i.e. it handed off rather than racing the still
    executing statement.
    """
    real = getattr(dbmod, conn_attr)
    assert real is not None, f"{conn_attr} singleton is not open"
    harness = _BlockedCheckpointHarness(real)
    monkeypatch.setattr(dbmod, conn_attr, harness.conn)

    result: dict[str, object] = {}

    def run_checkpoint():
        try:
            result["checkpoint"] = checkpoint_fn()
        except BaseException as exc:  # pragma: no cover - surfaced via assertions
            result["checkpoint_error"] = exc

    worker = threading.Thread(target=run_checkpoint, name="checkpoint")
    worker.start()
    interrupted_by_close = False
    elapsed = float("inf")
    try:
        assert harness.checkpoint_started.wait(timeout=_JOIN_TIMEOUT_S), "CHECKPOINT never started"
        started = time.perf_counter()
        close_fn()
        elapsed = time.perf_counter() - started
        # Sampled HERE, before the safety net below sets the same Event —
        # asserting on `harness.interrupted` afterwards would be vacuous.
        interrupted_by_close = harness.interrupted.is_set()
    finally:
        harness.interrupted.set()  # never leave the worker parked on a failure
        worker.join(timeout=_JOIN_TIMEOUT_S)

    assert interrupted_by_close, (
        f"{close_fn.__name__} must interrupt the in-flight CHECKPOINT cursor before closing "
        f"the parent — publish it via _register_interruptible_cursor and interrupt it in "
        f"{close_fn.__name__}, as the rolling-snapshot export does (#1294)"
    )
    assert elapsed < dbmod._INFLIGHT_STATEMENT_INTERRUPT_TIMEOUT_S, (
        f"{close_fn.__name__} spent its whole {dbmod._INFLIGHT_STATEMENT_INTERRUPT_TIMEOUT_S}s "
        f"interrupt budget ({elapsed:.1f}s) — the interrupt did not reach the cursor"
    )
    assert harness.parent_closed.is_set(), f"{close_fn.__name__} must still close the parent"
    assert not worker.is_alive(), "the checkpoint thread must unwind after the interrupt"
    assert "checkpoint_error" not in result, result.get("checkpoint_error")


class TestAnAbandonedCheckpointCursorIsNotClosedOutFromUnder:
    """#1294's contract, extended to the cursor #2352 introduced.

    Executing the CHECKPOINT outside `_system_db_lock` fixes the stall but
    moves the statement onto an abandonable child cursor. These pin the
    handoff that makes that safe, so a later refactor cannot quietly drop it.
    """

    def test_close_system_db_interrupts_an_in_flight_checkpoint(self, system_db, monkeypatch):
        _drive_close_against_an_in_flight_checkpoint(
            monkeypatch, "_system_db_conn", checkpoint_system_db, dbmod.close_system_db
        )

    def test_close_operational_db_interrupts_an_in_flight_checkpoint(self, system_db, monkeypatch):
        """`close_operational_db()` needed the handshake added, not widened.

        It had none: before #2352 its CHECKPOINT ran on the parent, so there
        was no child cursor to hand off. It is reachable by the same route as
        the system one — same loop, same abandoned thread — and on a Postgres
        app-state instance it is the only arm of that loop that does anything.
        """
        get_operational_db().close()
        _drive_close_against_an_in_flight_checkpoint(
            monkeypatch, "_operational_db_conn", checkpoint_operational_db, dbmod.close_operational_db
        )

    def test_the_interrupt_reaches_every_published_cursor_not_just_one(self, system_db):
        """Why the single slot became a registry.

        The export and the two CHECKPOINTs are separate publishers of one
        mechanism. If the interrupt only reached the most recent (or the
        first) publication, a close path would still race the other — so a
        second slot would have been a latent bug, and sharing one slot would
        have cleared the first publisher's idle state, which is what the close
        paths key their decision on.
        """

        class _Recorder:
            def __init__(self):
                self.interrupted = False

            def interrupt(self):
                self.interrupted = True

        first, second = _Recorder(), _Recorder()
        t1 = dbmod._register_interruptible_cursor(first)
        t2 = dbmod._register_interruptible_cursor(second)
        try:
            assert not dbmod._inflight_cursors_idle.is_set()
            # wait_s=0: one interrupt shot at everything, no waiting. Returns
            # False because neither fake ever reports itself finished.
            assert dbmod.interrupt_inflight_singleton_statements(caller="test") is False
            assert first.interrupted and second.interrupted
        finally:
            dbmod._unregister_interruptible_cursor(t1)
            assert not dbmod._inflight_cursors_idle.is_set(), "idle must not flip while one is left"
            dbmod._unregister_interruptible_cursor(t2)

        assert dbmod._inflight_cursors_idle.is_set()
        assert dbmod.interrupt_inflight_singleton_statements(caller="test") is True
