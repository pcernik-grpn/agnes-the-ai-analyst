### Changed
- **A table that has both a flat `data/<table>.parquet` and a `data/<table>/`
  partition directory now serves whichever of the two is FRESHER, and when the
  directory wins the stale flat parquet is deleted.** Both halves are a
  behavior change on live deployments. Previously the flat file won
  unconditionally, which froze distribution silently whenever the directory was
  the current data: the manifest advertised the table as a single file hashed
  from the stale pre-conversion copy, so `agnes pull` downloaded it, the md5
  matched, and analysts kept receiving stale data indefinitely while the
  server's own view read the fresh partitions. An affected table's manifest
  entry therefore changes shape (`parts` populated instead of a single hash)
  and every analyst re-pulls it once, as parts. In the mirror direction — a
  table flipped back to a flat write, leaving a stale partition directory —
  the flat file is the fresher one, keeps winning, and **nothing is deleted**.
  Freshness is compared by mtime, which is evidence and not a clock:
  filesystem granularity, a restored backup, a `cp` without `-p` or a stray
  `touch` can invert the verdict. When it is wrong, the manifest and the read
  surfaces are wrong together — they never disagree, because both call one
  comparator — and the loser is deleted only in the direction where the
  directory won; re-running the extract for that table restores the intended
  winner. A table whose partition directory holds no part yet is unaffected
  (that is a pending first sync, not a competing layout), a removal is never
  attempted before the partitioned data is actually publishable, and a removal
  that cannot be completed keeps surfacing on the row as `last_sync_error`
  naming both paths — as does the mirror direction, which does not self-heal.
  The delete itself goes through the same atomic-publish protocol used to write
  a parquet (publish first, then one `os.replace` onto a name no reader globs)
  and is refused unless the path validates as one safe segment inside the
  source's own `data/` directory.

### Internal
- One comparator, `src.parquet_publish.partition_dir_supersedes_flat`, is now
  the single definition of which parquet layout wins, shared by
  `src/orchestrator.py`'s manifest write and `app/utils.py`'s read resolvers —
  including the catalog profile refresh, which had been expressing the old rule
  by hand and could profile a table from the layout the read surfaces were not
  serving.
- `agnes pull`'s `_drop_stale_layout` now reclaims a superseded flat parquet
  through the shared `src.parquet_publish.retire_superseded_parquet` primitive
  instead of a bare `unlink`, so the client and the server make the same layout
  transition the same way — with the same path containment.
