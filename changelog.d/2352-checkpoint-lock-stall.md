### Fixed
- **A running sync no longer makes a DuckDB app-state instance unresponsive to
  every other request.** The periodic state-DB `CHECKPOINT` held the singleton
  connection lock across its own execution, and `get_system_db()` takes that
  same lock on every call — so while DuckDB flushed a grown write-ahead log
  (measured: 7.1 s for a 942 MB WAL, and a sync is what grows it), every
  authed request parked on the lock instead of the ~1.5 ms a state lookup
  costs. Both checkpoint accessors now take a cursor under the lock and run
  the statement outside it, the discipline the rolling-snapshot export already
  used: the same flush now completes with ~17 500 concurrent state reads served
  at 1.9 ms each. `checkpoint_operational_db()` had the identical defect and
  shares the same lock, which on a Postgres app-state instance stalled CLI
  login and Slack identity binding.

### Internal
- New measurement-backed guard (`tests/test_system_db_concurrency.py`) pinning
  that neither checkpoint accessor holds the singleton lock across execution,
  and recording the experiment that refuted the competing theory: DuckDB does
  *not* serialize statement execution across sibling cursors of one
  connection, so a sync's write stream — or an open write transaction — never
  queued readers behind it.
- Running a state-DB `CHECKPOINT` on a child cursor brings it under the same
  shutdown contract as the rolling-snapshot export: the single-slot
  interruptible-cursor machinery is now a registry, both checkpoint accessors
  publish into it, and every close path (`close_system_db`,
  `close_operational_db` — which had no handshake at all — and
  `close_singleton_connections`) interrupts and bounded-waits on whatever is
  in flight instead of closing the parent connection out from under it.
  Admitting a new child statement and beginning to close a singleton parent
  are also mutually exclusive now, so a close can no longer land in the gap
  between taking a cursor and publishing it — where the cursor is invisible to
  the drain — and close the parent out from under a statement that is about to
  run. Execution stays outside that handshake, exactly as it stays outside the
  singleton lock, and every acquisition of it is bounded: a publisher skips
  its best-effort tick rather than wait for a close, and a close proceeds
  rather than wait indefinitely for a publisher.
