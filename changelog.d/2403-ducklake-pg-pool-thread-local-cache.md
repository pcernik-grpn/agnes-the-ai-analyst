### Internal
- DuckLake sessions over a Postgres catalog now attach with the postgres
  extension's per-thread connection cache disabled
  (`SET GLOBAL pg_pool_enable_thread_local_cache = false` in
  `src/ducklake_session.py::_attach_ducklake`). With the cache on, an attach
  opened a second catalog backend whenever DuckDB's worker thread ran the
  nested catalog attach (1 attach in 4 on a loaded 2-CPU host — the source of
  the fleet-wide `test_pg_catalog_exactly_one_connection_per_attach` CI flake,
  #2403), and a closed session's cached connection outlived the DuckDB
  instance. One attach now holds exactly one backend at rest and
  `close_ducklake_sessions()` releases it; the sizing note in
  `docs/DEPLOYMENT.md` now states the per-attach pool cap concurrent
  statements can grow to.
