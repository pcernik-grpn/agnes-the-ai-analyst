"""Worker entry point for memory-isolated `profile_table` execution.

Invoked from `app/api/sync.py` profile loop via
``python -m src._profiler_worker`` with JSON args on stdin.

Why a separate worker process: ``profile_table`` opens a DuckDB
connection that allocates anon mmap arenas for query buffers (capped at
2 GiB by ``SET memory_limit`` since PR #434). When called repeatedly in
a loop inside the same Python interpreter, those arenas don't reliably
return to the OS between iterations — Python's allocator keeps them in
its free-list, libc's malloc keeps them in its heap, and after N
iterations the resident set has grown by ~N × peak-per-call even though
each individual call cleaned up its DuckDB session correctly. Running
each call as a separate subprocess means the OS reclaims **all** memory
on process exit, no fragmentation, no anon-arena retention.

Protocol:
- Reads a single JSON object from stdin with the keys:
    - ``table_name`` (str): logical name for the profile output
    - ``table_id`` (str): id field for the synthetic TableInfo
    - ``parquet_path`` (str): absolute path to the parquet file
      (single file or directory of partitioned parquets)
- Writes a single JSON object to stdout: the profile dict returned by
  ``profile_table``.
- Logs human-readable progress / errors to stderr (parent forwards).
- Exit code 0 on success, non-zero on failure (Python traceback on
  stderr + the parent's ``SubprocessJobError`` will surface it).

The caller (sync.py) is responsible for persisting the returned profile
via ``ProfileRepository.save(...)`` — the worker stays out of the
system.duckdb write path so DuckDB locking semantics remain simple
(one writer per file, all in the parent process).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s [profiler_worker]: %(message)s",
        stream=sys.stderr,
    )
    try:
        args = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"FATAL: stdin is not valid JSON: {e}", file=sys.stderr)
        return 2

    # Lazy import — keeps the cold-start light when this module is
    # discovered by tooling (e.g. mypy, ruff) without actually being
    # invoked.
    from src.profiler import profile_table, TableInfo

    table_name = args["table_name"]
    table_id = args["table_id"]
    parquet_path = Path(args["parquet_path"])

    table_info = TableInfo(name=table_name, table_id=table_id)
    profile = profile_table(table_info, parquet_path, [], {}, {})
    # default=str matches the parent-side profile writer (src/profiler.py) —
    # DECIMAL/NUMERIC columns yield decimal.Decimal stats that json.dumps
    # cannot serialize natively.
    print(json.dumps(profile, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
