"""Shared test fixtures for E2E tests."""

import contextlib as _contextlib
import hashlib as _hashlib
import logging
import os
import re as _re
import shutil as _shutil
import sys
import time as _time
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

# Ensure consistent JWT secret across all workers (pytest-xdist).
# Set at import time so every worker process picks up the same values
# before any module-level code in app.auth.jwt caches the secret.
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

# Ensure DATA_DIR-derived directories exist for modules that read DATA_DIR
# at import time (e.g. services/telegram_bot/config.py builds NOTIFICATIONS_DIR
# eagerly). The bot itself logs to stdout — there is no FileHandler anymore —
# but the directory still has to exist for the JSON state files.
import tempfile as _tf

# Per-xdist-worker isolation: the on-disk system.duckdb takes an exclusive
# file lock, so two workers sharing one DATA_DIR race on every
# get_system_db() open (sporadic "Could not set lock" failures whose
# incidence depends on test scheduling). The xdist controller imports this
# conftest first and its DATA_DIR is INHERITED by worker processes, so the
# worker suffix must be applied even when DATA_DIR is already set — but only
# when it points at our shared default, never at an operator-provided path.
#
# Per-CHECKOUT isolation on top of that: the worker suffix alone is not
# enough once a second git worktree runs its own suite concurrently, because
# `gw0` in worktree A and `gw0` in worktree B resolve to the SAME
# `$TMPDIR/.agnes-test-data/gw0/state/system.duckdb` — each worker really
# does write an 18-35 MB DB there — and DuckDB takes an exclusive file lock
# on it. Keying the root on a hash of this checkout's path gives every
# worktree its own tree, so `scripts/dev/worktree-spawn.sh` sessions can run
# tests at the same time. The hash (not the raw path) keeps the directory
# name short and free of separators.
_checkout_token = _hashlib.sha256(str(Path(__file__).resolve().parents[1]).encode()).hexdigest()[:8]
_default_data_dir = os.path.join(_tf.gettempdir(), f".agnes-test-data-{_checkout_token}")
_xdist_worker = os.environ.get("PYTEST_XDIST_WORKER", "")
if "DATA_DIR" not in os.environ:
    os.environ["DATA_DIR"] = _default_data_dir
if _xdist_worker and os.path.normpath(os.environ["DATA_DIR"]) == _default_data_dir:
    os.environ["DATA_DIR"] = os.path.join(_default_data_dir, _xdist_worker)
os.makedirs(os.path.join(os.environ["DATA_DIR"], "notifications"), exist_ok=True)
os.makedirs(os.path.join(os.environ["DATA_DIR"], "state"), exist_ok=True)

# ---------------------------------------------------------------------------
# Local worker cap
# ---------------------------------------------------------------------------
# `-n auto` resolves to one worker per core, and each worker is a full Python
# process that imports `app.main` — measured at ~430 MB RSS once warm (pandas
# and sqlalchemy are pulled in eagerly). On a 14-core laptop that is ~6.5 GB
# and 14 cores pinned for a suite that is mostly waiting on DuckDB file I/O,
# which is what makes the machine unusable and drains the battery. Worse, a
# second git worktree running its own suite doubles it.
#
# Past ~6 workers the suite is I/O-bound, not CPU-bound, so the extra
# processes buy little wall-clock while costing linear RAM and heat. Capping
# what `auto` MEANS (rather than editing the documented `-n auto` command)
# keeps one command working everywhere and leaves every explicit `-n N`
# untouched.
#
# CI is exempt: its runners have 2-4 cores, `auto` is already small there, and
# shard wall-clock is the thing being optimized. `AGNES_TEST_MAX_WORKERS`
# overrides the cap; `PYTEST_XDIST_AUTO_NUM_WORKERS` set by the caller always
# wins, since we only fill it in when absent.
LOCAL_MAX_WORKERS = 6

if "PYTEST_XDIST_AUTO_NUM_WORKERS" not in os.environ and not os.environ.get("CI"):
    _cap = os.environ.get("AGNES_TEST_MAX_WORKERS", "").strip()
    try:
        _cap_n = int(_cap) if _cap else LOCAL_MAX_WORKERS
    except ValueError:
        _cap_n = LOCAL_MAX_WORKERS
    if _cap_n > 0:
        os.environ["PYTEST_XDIST_AUTO_NUM_WORKERS"] = str(max(1, min(_cap_n, os.cpu_count() or _cap_n)))

# ---------------------------------------------------------------------------
# Small DuckDB blocks for every test-created database
# ---------------------------------------------------------------------------
# DuckDB allocates storage in 256 KiB blocks by default, so a freshly created
# system.duckdb (~217 CREATE TABLEs at the current schema version) weighs
# ~7 MB before a single row of test data. Multiplied across the suite this
# dominated basetemp: a retained full-run dir measured 51 GB, of which 47 GB
# was 5,126 per-test system.duckdb files (~9 MB average). 16 KiB blocks cut
# the fresh file to ~3.7 MB and — the DDL being I/O-bound — schema init from
# ~0.95 s to ~0.28 s per fresh DB.
#
# Wrapped HERE in the test harness rather than env-gated inside
# src/duckdb_conn._open_duckdb: DuckDB refuses a second connection to the
# same path with a different config ("Can't open a connection to same
# database file with a different configuration than existing connections"),
# so the option must reach EVERY duckdb.connect() in the process — direct
# connects in tests included — or none of them. Patching the module
# attribute before any src/ import guarantees that consistency and leaves
# production code untouched. Subprocess-spawned CLIs still create
# default-block DBs; those are a small minority of the suite's databases.
#
# ``AGNES_TEST_DUCKDB_BLOCK_SIZE`` overrides the size — any power of two in
# DuckDB's accepted [16384, 262144] range — and ``0`` disables the wrapper.
_TEST_DUCKDB_BLOCK_SIZE = os.environ.get("AGNES_TEST_DUCKDB_BLOCK_SIZE", "16384")

if _TEST_DUCKDB_BLOCK_SIZE != "0" and not getattr(duckdb.connect, "_agnes_small_blocks", False):
    _orig_duckdb_connect = duckdb.connect

    def _connect_with_small_blocks(*args, **kwargs):
        # `config` is only ever passed as a keyword in this codebase; if a
        # caller someday passes it positionally (3rd arg), stay out of the way.
        if len(args) < 3:
            config = dict(kwargs.get("config") or {})
            config.setdefault("default_block_size", _TEST_DUCKDB_BLOCK_SIZE)
            kwargs["config"] = config
        return _orig_duckdb_connect(*args, **kwargs)

    _connect_with_small_blocks._agnes_small_blocks = True  # type: ignore[attr-defined]
    duckdb.connect = _connect_with_small_blocks

# Real-home shell configs that `agnes init` (cli/lib/shortcut.py) can append
# launcher blocks to. Resolved at import time — before any test monkeypatches
# HOME — so the guard below always watches the developer's *actual* rc files,
# not a per-test fake home.

_REAL_HOME = Path(os.path.expanduser("~"))
_GUARDED_SHELL_CONFIGS = (
    _REAL_HOME / ".zshrc",
    _REAL_HOME / ".bashrc",
    _REAL_HOME / ".bash_profile",
    _REAL_HOME / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
    _REAL_HOME / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
)
# The launcher install dir (`~/.local/bin`) is watched by *name listing* only:
# a leaking test drops a new script named after its tmp workspace there.
_REAL_LOCAL_BIN = _REAL_HOME / ".local" / "bin"


# ---------------------------------------------------------------------------
# Pre-flight disk guard
# ---------------------------------------------------------------------------
# With `tmp_path_retention_policy = failed` (pytest.ini), a PASSING test's tmp
# dir is deleted at its own teardown, so a healthy full run's basetemp peak is
# only failures + in-flight tests + session-scoped fixture dirs — single-digit
# GB, where retaining every dir until session end used to accumulate ~38-40 GB
# per run. The guard survives the collapse because the collapse is conditional
# on tests passing: a broken environment that fails tests wholesale retains
# every failing test's dir and trends back toward the old sum-of-all-tests
# footprint. When the disk runs out mid-run the suite does not fail cleanly:
# every teardown raises `OSError: [Errno 28]`, burying the real result under
# thousands of errors, and the machine is left wedged at 100% full.
#
# Thresholds are tuned so the abort floor only catches runs that are already
# doomed. CI shards the suite eight ways (`--splits 8`) and runs far leaner than
# a single local invocation — a local-sized threshold must never abort a shard.
# The number has come down twice, each time because the footprint it guarded
# against shrank: 60 → 30 once every test-created DuckDB moved to 16 KiB blocks
# (~28 GB in-flight for a full local run), then 30 → 10 once passing tests stop
# retaining their tmp dirs at all (~0.4 GB peak). What 10 GB buys now is room
# for the dirs *failing* tests keep, not for the run itself.
DISK_WARN_GB = 10
DISK_ABORT_GB = 5


def disk_guard_verdict(*, free_bytes: int) -> tuple[str, str]:
    """Classify free space as ``ok`` / ``warn`` / ``abort`` plus a message.

    Pure so it can be tested without a real filesystem; the caller supplies
    ``free_bytes``. Set ``AGNES_SKIP_DISK_CHECK=1`` to bypass entirely.
    """
    if os.environ.get("AGNES_SKIP_DISK_CHECK", "").strip().lower() not in ("", "0", "false"):
        return "ok", ""

    free_gb = free_bytes / (1024**3)

    if free_gb < DISK_ABORT_GB:
        return "abort", (
            f"Only {free_gb:.1f} GB free on the pytest basetemp filesystem "
            f"({_tf.gettempdir()}). A full run wants ~{DISK_WARN_GB} GB of "
            f"headroom (failing tests retain their tmp dirs) and will fill "
            f"the disk, producing thousands of Errno 28 teardown errors "
            f"instead of a usable result.\n"
            f"  Free space, then re-run. Stale fixtures from previous runs:\n"
            f"    rm -rf {os.path.join(_tf.gettempdir(), 'pytest-of-*')}\n"
            f"  Override with AGNES_SKIP_DISK_CHECK=1 if you know better."
        )

    if free_gb < DISK_WARN_GB:
        return "warn", (
            f"Low disk: {free_gb:.1f} GB free, a full run wants ~{DISK_WARN_GB} GB "
            f"of basetemp headroom (failing tests retain their tmp dirs). "
            f"Consider running a subset, or clean up with "
            f"`rm -rf {os.path.join(_tf.gettempdir(), 'pytest-of-*')}`."
        )

    return "ok", ""


def pytest_sessionstart(session):
    """Check headroom once, in the controller, before any test runs."""
    # xdist workers inherit the controller's verdict — checking per worker would
    # print the same warning N times and abort mid-fan-out.
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return

    try:
        usage = _shutil.disk_usage(_tf.gettempdir())
    except OSError:
        return  # never let the guard itself break a run

    verdict, message = disk_guard_verdict(free_bytes=usage.free)
    if verdict == "abort":
        pytest.exit(f"\ndisk guard: {message}", returncode=1)
    if verdict == "warn":
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"disk guard: {message}", yellow=True)

    # Sweep leaked scratch from previous runs BEFORE this one allocates any.
    # Both sweeps used to be reachable only from ``tests/db_pg``'s own session
    # fixture, so a developer who never ran the PG package (the common case:
    # `pytest tests/test_foo.py`) accumulated orphans indefinitely — measured
    # at 4.1 GB of pgserver dirs plus 0.8 GB of stale DATA_DIR roots on one
    # machine. Running it from the controller's sessionstart makes every
    # invocation of the suite pay the (millisecond) scan and keeps the
    # footprint bounded. Never fatal: this is hygiene, not a gate.
    _sweep_leaked_scratch(session)


def _sweep_leaked_scratch(session) -> None:
    """Reap orphaned pgserver data dirs + stale per-checkout DATA_DIR roots."""
    tmp_root = Path(_tf.gettempdir())
    reaped: list[Path] = []
    try:
        from tests.db_pg.pgserver_reaper import reap_orphaned_pgserver_dirs

        reaped.extend(reap_orphaned_pgserver_dirs(tmp_root))
    except Exception:
        pass  # psutil missing, permissions, racing session — never break a run
    try:
        reaped.extend(reap_stale_test_data_roots(tmp_root))
    except Exception:
        pass
    if reaped:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"swept {len(reaped)} leaked scratch dir(s) from previous runs", yellow=True)


# Roots older than this are certainly not serving a live run: an active
# session rewrites its worker DBs continuously, so a fresh mtime means "in
# use". Deliberately generous — a stale root costs disk, a wrongly deleted
# one breaks a concurrent worktree's suite mid-run.
STALE_DATA_ROOT_SECONDS = 24 * 3600


def reap_stale_test_data_roots(tmp_root: Path, *, max_age_seconds: int = STALE_DATA_ROOT_SECONDS) -> list[Path]:
    """Remove ``.agnes-test-data*`` roots left by runs that are long gone.

    Covers this checkout's own abandoned roots, other checkouts' roots (a
    deleted worktree never cleans up after itself), and the legacy un-suffixed
    ``.agnes-test-data`` from before the per-checkout token existed. The
    CURRENT run's root is never a candidate: it was just created, so its mtime
    is now.
    """
    removed: list[Path] = []
    now = _time.time()
    current = os.path.normpath(os.environ.get("DATA_DIR", ""))
    for d in tmp_root.glob(".agnes-test-data*"):
        try:
            if not d.is_dir():
                continue
            if os.path.normpath(str(d)) == current or current.startswith(os.path.normpath(str(d)) + os.sep):
                continue  # this run's own root (or its worker subdir's parent)
            if now - _newest_mtime(d) < max_age_seconds:
                continue
            _shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
        except OSError:
            continue
    return removed


def _newest_mtime(root: Path) -> float:
    """Most recent mtime anywhere under ``root``.

    The root dir's own mtime only tracks direct-child creation, so a long run
    writing into ``gw3/state/system.duckdb`` leaves it stale and would look
    abandoned. Walk one worker level deep — enough to see liveness without
    paying a full recursive stat on a multi-GB tree.
    """
    newest = root.stat().st_mtime
    for child in root.iterdir():
        try:
            newest = max(newest, child.stat().st_mtime)
            if child.is_dir():
                for grandchild in child.iterdir():
                    newest = max(newest, grandchild.stat().st_mtime)
        except OSError:
            continue
    return newest


# ---------------------------------------------------------------------------
# Post-success basetemp sweep
# ---------------------------------------------------------------------------
# The retention settings in pytest.ini bound how many PAST sessions survive,
# but a passing run still leaves its own basetemp behind until the NEXT run's
# startup sweep. Deleting it right after a fully green session costs nothing —
# the artifacts exist for debugging failures, and there are none. Failed or
# interrupted runs keep their dir (covered by the retention sweep), and
# ``AGNES_KEEP_BASETEMP=1`` keeps even a passing run's.
#
# ``tmp_path_retention_policy = failed`` now handles the bulk of it per-test,
# so what reaches this sweep is the remainder: session-scoped fixture dirs and
# anything a failing test kept before a later green run. The two are not
# redundant — the policy cannot delete a dir the session still holds, and this
# cannot delete a dir mid-run.
#
# It is still deliberately NOT ``tmp_path_retention_count = 0``, whose startup
# sweep deletes the CURRENT session's dir mid-run. Session end in the
# controller runs after every teardown, so no live handle can be desynced —
# which is the same invariant ``_close_tmp_handles`` establishes per-test
# to make the retention policy safe.


def basetemp_sweep_verdict(
    *,
    exitstatus: int,
    is_xdist_worker: bool,
    user_basetemp: bool,
    basetemp: Path | None,
    keep_env: str = "",
) -> bool:
    """Decide whether the just-finished session's basetemp should be removed.

    Pure so it can be tested without a real session; the caller supplies the
    session facts. Only a fully passing run (``exitstatus == 0``), judged in
    the xdist controller, using pytest's own numbered ``pytest-of-*/pytest-N``
    directory (never a user-specified ``--basetemp``), is swept.
    """
    if is_xdist_worker:
        return False
    if keep_env.strip().lower() not in ("", "0", "false"):
        return False
    if exitstatus != 0:
        return False
    if user_basetemp:
        return False
    if basetemp is None:
        return False
    # Belt and suspenders: only ever delete pytest's own numbered dirs.
    if not _re.fullmatch(r"pytest-\d+", basetemp.name):
        return False
    if not basetemp.parent.name.startswith("pytest-of-"):
        return False
    return True


def pytest_sessionfinish(session, exitstatus):
    """Remove the current session's basetemp after a fully green run."""
    factory = getattr(session.config, "_tmp_path_factory", None)
    # Read the private attr instead of calling getbasetemp() — the getter
    # would CREATE the directory in a run that never touched tmp_path.
    basetemp = getattr(factory, "_basetemp", None) if factory is not None else None
    if not basetemp_sweep_verdict(
        exitstatus=exitstatus,
        is_xdist_worker=bool(os.environ.get("PYTEST_XDIST_WORKER")),
        user_basetemp=bool(session.config.option.basetemp),
        basetemp=basetemp,
        keep_env=os.environ.get("AGNES_KEEP_BASETEMP", ""),
    ):
        return
    # Workers have finished their teardowns by the time the controller gets
    # here; a worker process still holding an open handle is harmless on
    # POSIX (unlink succeeds), and ignore_errors covers the rest.
    _shutil.rmtree(basetemp, ignore_errors=True)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            f"basetemp swept after green run: {basetemp} (AGNES_KEEP_BASETEMP=1 to keep)",
        )


def _shell_config_fingerprints() -> dict:
    fps = {}
    for path in _GUARDED_SHELL_CONFIGS:
        try:
            fps[path] = _hashlib.sha256(path.read_bytes()).hexdigest()
        except FileNotFoundError:
            fps[path] = None
        except OSError:
            # Unreadable (permissions, etc.) — treat as opaque-but-stable.
            fps[path] = "<unreadable>"
    try:
        fps[_REAL_LOCAL_BIN] = tuple(sorted(os.listdir(_REAL_LOCAL_BIN)))
    except OSError:
        fps[_REAL_LOCAL_BIN] = None
    return fps


@pytest.fixture(autouse=True)
def _guard_real_shell_config():
    """Fail any test that mutates the developer's real shell rc files.

    Tests that exercise `agnes init` / `install_launcher_shortcut` must
    redirect writes into tmp (monkeypatch.setenv("HOME", ...) for in-process
    calls, env["HOME"] = <tmp> for subprocess calls) or pass --no-shortcut.
    Forgetting either silently appends per-test launcher blocks to the
    developer's real ~/.zshrc — this guard turns that leak into a loud
    failure at the offending test.

    Under pytest-xdist a leak in a concurrently running test on another
    worker can, rarely, be blamed on the wrong test — but any failure here
    still means some test in the run is leaking.
    """
    before = _shell_config_fingerprints()
    yield
    after = _shell_config_fingerprints()
    changed = [str(p) for p in before if before[p] != after[p]]
    if changed:
        pytest.fail(
            "This test wrote to the developer's REAL shell config / launcher dir: "
            + ", ".join(changed)
            + ". Redirect HOME into tmp (monkeypatch.setenv('HOME', str(tmp_path)) "
            "or env['HOME'] for subprocesses) or pass --no-shortcut to `agnes init`."
        )


@pytest.fixture(autouse=True)
def _flea_guardrails_disabled_by_default(monkeypatch):
    """Default flea-market upload pipeline to OFF for every test.

    Post-v45 publish-gate refactor split operator intent
    (``guardrails.enabled`` in instance.yaml) from provider readiness
    (``ANTHROPIC_API_KEY`` in env). Both default to True/False in a
    test env that has no instance.yaml + no key — so the gate is now
    ``enabled=True, ready=False`` and every upload sits at
    ``visibility_status='pending'`` waiting on a non-existent LLM
    call. That breaks every legacy test that uploads a bundle and
    expects v1 to be live.

    Default both to False here so legacy tests keep working. Tests
    that exercise the guardrail-on path override per-test with
    ``monkeypatch.setattr("app.api.store.get_guardrails_enabled",
    lambda: True)`` + the matching ``..._llm_provider_ready`` line.
    """
    try:
        # `app.api.store` does a top-level import — patch the bound
        # symbol there. Existing per-test overrides target the same path.
        monkeypatch.setattr(
            "app.api.store.get_guardrails_enabled",
            lambda: False,
        )
    except (AttributeError, ImportError):
        # app.api.store may not be importable in some test contexts
        # (e.g. tests that exercise migrations without the full app).
        pass
    try:
        # `app.api.admin` does a function-local import — patch the
        # source so per-call lookups see the override.
        monkeypatch.setattr(
            "app.instance_config.get_guardrails_enabled",
            lambda: False,
        )
    except (AttributeError, ImportError):
        pass


@pytest.fixture(autouse=True)
def _disable_auth_rate_limit_in_tests():
    """Disable the slowapi auth rate limiter for every test by default.

    Production limits (e.g. 10/minute on /auth/password/login) would otherwise
    bleed into test files that hammer auth endpoints in tight loops — those
    tests existed long before the limiter and shouldn't have to know about
    its bucket sizes. The dedicated rate-limit test in test_auth_rate_limit.py
    flips ``limiter.enabled = True`` and resets state inside its own scope.
    """
    from app.auth.rate_limit import limiter

    was_enabled = limiter.enabled
    limiter.enabled = False
    try:
        limiter.reset()
    except Exception:
        # In-memory backend always resets cleanly; defensive guard for
        # third-party storage backends operators might wire in later.
        pass
    yield
    limiter.enabled = was_enabled


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """Reset module-level caches that survive across tests on the same
    pytest-xdist worker process. Without this, a test that populates
    `app.instance_config._instance_config` (e.g. via `runpy.run_module`
    in test_bigquery_extractor's __main__ tests, or via any path that
    calls `app.instance_config.get_value`) leaves stale config visible
    to the next test on that worker — including config that points at
    a different DATA_DIR than the next test's e2e_env set.

    Caches reset:
    - app.instance_config._instance_config — instance.yaml deep-merge cache
    - get_bq_access (functools.cache) — BqAccess(BqProjects(...)) lru
    - app.api.v2_quota._quota_singleton — per-user quota tracker

    Pre-existing flakiness; surfaced by issue #160 PR #168 shifting the
    test bucket distribution on xdist worker gw2.
    """
    try:
        import app.instance_config as _ic

        _ic._instance_config = None
        try:
            from connectors.bigquery.access import get_bq_access

            get_bq_access.cache_clear()
        except (ImportError, AttributeError):
            pass
    except ImportError:
        pass
    try:
        import app.api.v2_quota as _q

        _q._quota_singleton = None
    except ImportError:
        pass
    # Backend-state parse-once cache — process-global, so a test
    # that reads/writes one overlay would otherwise leave a stale
    # (BackendState, url) visible to the next test on this xdist worker.
    try:
        from src.db_state_machine import reset_backend_state_cache

        reset_backend_state_cache()
    except ImportError:
        pass
    try:
        from app.api import v2_catalog as _vc

        _vc._table_rows_cache.clear()
    except (ImportError, AttributeError):
        pass
    # Schema TTL cache — keyed on table_id (plus the policy identity for a
    # policied table) with a 1h TTL, so two suites registering the SAME id
    # with different columns hand each other the wrong schema. Surfaced when
    # the Databricks scan tests registered `dbx.sales.orders_raw` with two
    # columns and the remote-query suite's one-column assertion began failing
    # on file ordering alone.
    try:
        from app.api import v2_schema as _vs

        _vs._schema_cache.clear()
    except (ImportError, AttributeError):
        pass
    try:
        import app.api.cache_warmup as _cw

        _cw.WARMUP_STATE = None
    except (ImportError, AttributeError):
        pass
    # DuckDB parse-oracle health latch. Sticky by design: once a process
    # decides the engine answers wrongly, it never re-probes. A leaked False
    # would fail SILENTLY GREEN — the SQL name guards fall back to the text
    # scan, which is conservative enough that every deny test still passes
    # while none of them exercises the oracle those tests exist to cover.
    try:
        import app.api.query as _q_mod

        _q_mod._ORACLE_HEALTHY = None
    except (ImportError, AttributeError):
        pass
    yield
    try:
        from app.api import v2_catalog as _vc

        _vc._table_rows_cache.clear()
    except (ImportError, AttributeError):
        pass
    # Schema TTL cache — keyed on table_id (plus the policy identity for a
    # policied table) with a 1h TTL, so two suites registering the SAME id
    # with different columns hand each other the wrong schema. Surfaced when
    # the Databricks scan tests registered `dbx.sales.orders_raw` with two
    # columns and the remote-query suite's one-column assertion began failing
    # on file ordering alone.
    try:
        from app.api import v2_schema as _vs

        _vs._schema_cache.clear()
    except (ImportError, AttributeError):
        pass
    try:
        import app.api.cache_warmup as _cw

        _cw.WARMUP_STATE = None
    except (ImportError, AttributeError):
        pass


def handle_outlives_tmp(path: str | None, basetemp: str) -> bool:
    """Whether a process-global handle's file path must be released at test
    teardown.

    True when the path points into pytest's basetemp (its tmp dir is deleted
    by ``tmp_path_retention_policy = failed`` on pass, and pytest REUSES the
    freed numbered dir name for the next same-named test — so a surviving
    handle desyncs path-keyed state from disk), or when its parent dir is
    already gone (same desync via any other tmp scheme). False for the shared
    default DATA_DIR, whose handles legitimately span tests. Pure so it can
    be tested without touching the real singletons. Only for filesystem
    paths — the parent-dir heuristic misfires on DSN strings.
    """
    if not path:
        return False
    path = str(path)
    if path.startswith(basetemp.rstrip(os.sep) + os.sep):
        return True
    return not os.path.isdir(os.path.dirname(path))


def close_tmp_handles(basetemp: str) -> None:
    """Release every process-global handle that points into ``basetemp``.

    Teardown body of the ``_close_tmp_handles`` autouse fixture, factored
    out so tests/test_tmp_db_singleton_guard.py can drive it directly. Three
    handle classes, all real prior failures of the same invariant:

    - **Root-logger FileHandlers** — e.g. the corporate-memory collector's
      ``main()`` adds one under DATA_DIR and never removes it; a test
      calling it under a tmp DATA_DIR leaked a handler that bled every later
      log record into that test's dir, and once retention deletes the dir,
      the next root-logger emit raises FileNotFoundError out of
      ``logger.info()`` (FileHandler's lazy ``_open`` is outside logging's
      error-swallowing) — CI shard-5 failure on PR #1370.
    - **src/db.py DuckDB singletons** — the original `Duplicate key
      "id: admin1"` desync (see pytest.ini). The path globals are reset
      after closing: ``close_singleton_connections()`` clears only the
      connections, and a stale tmp path would otherwise re-trigger this
      close on every later test, needlessly bouncing singletons that live
      on the shared default DATA_DIR (Devin review on PR #1370).
    - **src/ducklake_session singletons** — keyed by ``(catalog_dsn,
      data_path)``; a file-catalog session under a tmp DATA_DIR desyncs the
      same way. Key elements are matched by basetemp prefix only (a
      Postgres DSN is not a filesystem path). ``close_ducklake_sessions()``
      resets its own keys.
    """
    prefix = basetemp.rstrip(os.sep) + os.sep

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        base_filename = getattr(handler, "baseFilename", None)
        if base_filename and handle_outlives_tmp(str(base_filename), basetemp):
            root_logger.removeHandler(handler)
            with _contextlib.suppress(Exception):
                handler.close()

    _db = sys.modules.get("src.db")
    if _db is not None and any(
        handle_outlives_tmp(p, basetemp)
        for p in (_db._system_db_path, _db._operational_db_path, _db._analytics_db_path)
    ):
        _db.close_singleton_connections()
        _db._system_db_path = None
        _db._operational_db_path = None
        _db._analytics_db_path = None

    _dl = sys.modules.get("src.ducklake_session")
    if _dl is not None and any(
        element and str(element).startswith(prefix)
        for key in (_dl._read_key, _dl._write_key, _dl._shared_file_key)
        for element in (key or ())
    ):
        _dl.close_ducklake_sessions()


@pytest.fixture(autouse=True)
def _close_tmp_handles(tmp_path_factory):
    """Release process-global handles (DuckDB singletons, DuckLake sessions,
    root-logger FileHandlers) that point into pytest's basetemp, so no live
    handle outlives its tmp_path dir.

    This is the invariant that makes ``tmp_path_retention_policy = failed``
    (pytest.ini) safe: that policy deletes each PASSING test's tmp_path in
    its teardown, and pytest then hands the same numbered dir name to the
    next test with the same (truncated) name. ``get_system_db()`` reopens
    only on path CHANGE, so without this close the stale handle over the
    unlinked file keeps serving — and the next seeded_app fixture seeds
    into yesterday's DB: `Duplicate key "id: admin1"` (~57 spurious errors
    across the suite, the failure mode that forced the policy back to
    ``all`` before this fixture existed).

    Handles on the shared default DATA_DIR are left open — closing them
    every test would re-run schema checks for no benefit, and their dir is
    never deleted mid-session. See ``close_tmp_handles`` for the handle
    classes and the per-class rationale.
    """
    yield
    close_tmp_handles(str(tmp_path_factory.getbasetemp()))


@pytest.fixture
def e2e_env(tmp_path, monkeypatch):
    """Set up complete E2E environment with DATA_DIR, create dirs."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    (tmp_path / "extracts").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "state").mkdir()

    yield {
        "data_dir": tmp_path,
        "extracts_dir": tmp_path / "extracts",
        "analytics_db": str(tmp_path / "analytics" / "server.duckdb"),
    }


def create_mock_extract(extracts_dir: Path, source_name: str, tables: list[dict]):
    """Create a mock extract.duckdb with _meta and data tables.

    tables: [{"name": "orders", "data": [{"id": "1", "total": "100"}], "query_mode": "local"}]
    """
    source_dir = extracts_dir / source_name
    source_dir.mkdir(exist_ok=True)
    data_dir = source_dir / "data"
    data_dir.mkdir(exist_ok=True)

    db_path = source_dir / "extract.duckdb"
    conn = duckdb.connect(str(db_path))

    conn.execute("""CREATE TABLE IF NOT EXISTS _meta (
        table_name VARCHAR, description VARCHAR, rows BIGINT,
        size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR DEFAULT 'local'
    )""")
    # Delete existing meta rows to allow re-calling
    conn.execute("DELETE FROM _meta")

    for t in tables:
        name = t["name"]
        rows_data = t.get("data", [])
        query_mode = t.get("query_mode", "local")

        if rows_data and query_mode == "local":
            # Write actual parquet file
            pq_path = str(data_dir / f"{name}.parquet")
            # Build SQL from data, preserving Python scalar types.
            selects = []
            for row in rows_data:
                parts = []
                for k, v in row.items():
                    if isinstance(v, bool):
                        parts.append(f"CAST('{v}' AS BOOLEAN) AS {k}")
                    elif isinstance(v, int):
                        parts.append(f"CAST('{v}' AS BIGINT) AS {k}")
                    elif isinstance(v, float):
                        parts.append(f"CAST('{v}' AS DOUBLE) AS {k}")
                    elif v is None:
                        parts.append(f"NULL AS {k}")
                    else:
                        parts.append(f"'{v}' AS {k}")
                vals = ", ".join(parts)
                selects.append(f"SELECT {vals}")
            union_sql = " UNION ALL ".join(selects)
            conn.execute(f"COPY ({union_sql}) TO '{pq_path}' (FORMAT PARQUET)")

            rows = len(rows_data)
            size = os.path.getsize(pq_path)
            conn.execute(f"CREATE OR REPLACE VIEW \"{name}\" AS SELECT * FROM read_parquet('{pq_path}')")
            conn.execute(
                "INSERT INTO _meta VALUES (?, ?, ?, ?, current_timestamp, 'local')",
                [name, t.get("description", ""), rows, size],
            )
        else:
            # Remote or empty table
            conn.execute(f'CREATE TABLE IF NOT EXISTS "{name}" (id VARCHAR)')
            conn.execute(
                "INSERT INTO _meta VALUES (?, ?, 0, 0, current_timestamp, ?)",
                [name, t.get("description", ""), query_mode],
            )

    conn.close()
    return db_path


def write_test_parquet(path: str, data: list[dict]):
    """Create a parquet file from list of dicts.

    NB: every value is written as a string. This deliberately does NOT match
    ``create_mock_extract`` above, which preserves Python scalar types so a
    fixture can produce a non-VARCHAR column. Fixtures that care about column
    type — anything exercising type-aware behaviour such as policy redaction —
    want that helper, not this one.
    """
    conn = duckdb.connect()
    selects = []
    for row in data:
        vals = ", ".join(f"'{v}' AS {k}" for k, v in row.items())
        selects.append(f"SELECT {vals}")
    union_sql = " UNION ALL ".join(selects)
    conn.execute(f"COPY ({union_sql}) TO '{path}' (FORMAT PARQUET)")
    conn.close()


@pytest.fixture(scope="session")
def _shared_seeded_app():
    """Build the FastAPI app ONCE for the whole test session and hand it to
    every ``seeded_app`` user.

    ``create_app()`` measured ~343ms warm — ~90 ``include_router`` calls plus
    middleware setup — against a ~380ms total ``seeded_app`` fixture cost, so
    rebuilding it per test (9.7k call sites across the suite) was ~90% waste.
    Every DB access goes through ``get_system_db()`` / the ``*_repo()``
    factories, which read ``os.environ["DATA_DIR"]`` at CALL time and reopen on
    path change (see ``src/db.py``), so per-test isolation is unaffected by
    which app instance served the request — only by which DATA_DIR was active
    when the request ran, which ``e2e_env`` still sets per-test via
    ``monkeypatch``.

    TWO THINGS DO BIND TO A DATA_DIR AT CONSTRUCTION, and this fixture is
    session-scoped without depending on ``e2e_env``, so they bind to whatever
    DATA_DIR is active when pytest first builds it — NOT to the requesting
    test's ``tmp_path``:

    - ``/uploads`` is mounted as ``StaticFiles(directory=${DATA_DIR}/uploads)``
      (``app/main.py``), and the path is frozen into the mount.
    - the session secret is read from ``${DATA_DIR}/state/.session_secret``
      (``app/secrets.py::get_session_secret``), so a session cookie signed
      under one test validates under another.

    No current ``seeded_app`` test GETs ``/uploads/...``, which is why this is
    invisible today; ``test_shared_app_uploads_binding.py`` is the ratchet that
    keeps it that way. A test that needs the uploads mount rooted in its own
    DATA_DIR must use ``seeded_app_fresh``, which builds the app after
    ``e2e_env`` has run.

    ``load_instance_config(strict=True)`` runs once here, at whatever
    DATA_DIR is active for the first caller — but the per-test
    ``_reset_module_caches`` autouse fixture nulls
    ``app.instance_config._instance_config`` before every test regardless,
    so every test's first ``get_value()`` call re-reads from ITS OWN
    DATA_DIR/state/instance.yaml at request time. Boot-time refusal on a
    corrupt overlay (``InstanceConfigUnreadable``) is exercised by tests that
    call ``create_app()`` directly, not through this fixture, so that
    behaviour still gets a fresh app.

    Two attributes DO get mutated post-construction by a couple of tests —
    ``app.state.chat_config`` (lifespan never runs under a bare
    ``TestClient(app)``, so tests set it by hand: test_web_admin_nav.py,
    test_web_chat_empty_state.py) and, defensively, ``app.dependency_overrides``
    (no current seeded_app test uses it, but the pattern exists elsewhere in
    this file). ``seeded_app`` snapshots ``app.state`` right after
    construction and restores both on every test's teardown — see below.
    """
    from app.main import create_app

    app = create_app()
    pristine_state = dict(app.state._state)
    return app, pristine_state


def _seed_users_and_mint_tokens(app) -> dict:
    """Seed the four legacy-role test users + mint their JWTs against
    ``app``, and wrap it in a ``TestClient``. Shared body for ``seeded_app``
    (session-shared app) and ``seeded_app_fresh`` (function-scoped fresh
    app) — everything except the app object itself is identical between
    them.

    v13: roles are no longer the auth source of truth. The admin user is
    placed in the Admin user_group; the others are Everyone-only members.
    Tokens for km_admin and viewer are kept so role-gating regression tests
    that still reference them keep passing — gate semantics still match
    where it matters (admin bypass, dataset_permissions checks).
    """
    from fastapi.testclient import TestClient

    from app.auth.jwt import create_access_token
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    repo = UserRepository(conn)
    repo.create(id="admin1", email="admin@test.com", name="Admin")
    repo.create(id="km_admin1", email="km@test.com", name="KM Admin")
    repo.create(id="analyst1", email="analyst@test.com", name="Analyst")
    repo.create(id="viewer1", email="viewer@test.com", name="Viewer")

    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member(
        "admin1",
        admin_gid,
        source="system_seed",
    )
    conn.close()

    return {
        "client": TestClient(app),
        "admin_token": create_access_token("admin1", "admin@test.com"),
        "km_admin_token": create_access_token("km_admin1", "km@test.com"),
        "analyst_token": create_access_token("analyst1", "analyst@test.com"),
        "viewer_token": create_access_token("viewer1", "viewer@test.com"),
    }


@pytest.fixture
def seeded_app(e2e_env, _shared_seeded_app):
    """FastAPI TestClient with seeded users + JWT tokens for all four legacy
    role tokens (admin, km_admin, analyst, viewer).

    The FastAPI app object is session-shared (``_shared_seeded_app``) — only
    the DB seed, DATA_DIR and TestClient below are fresh per test. Teardown
    restores ``app.state`` to its pristine post-``create_app()`` snapshot and
    clears ``app.dependency_overrides``, so a test that mutates either (e.g.
    setting ``app.state.chat_config`` because lifespan never runs under a
    bare ``TestClient``) cannot leak into the next test.

    Do NOT enter this fixture's ``client`` as a context manager (``with
    seeded_app["client"] as client:``) — that runs the real ASGI *lifespan*,
    which starts the streamable MCP session manager
    (``app/api/mcp_streamable.py``). The SDK allows that manager's
    ``run()`` to be entered at most once per instance and never restarted;
    since the app (and therefore its one ``mcp_streamable_instance``) is now
    shared across the whole session, a second lifespan entry on a later test
    finds the task group already torn down from the first and raises
    ``RuntimeError: Task group is not initialized`` (or a stale-event-loop
    error on the manager's internal locks). Tests that must run lifespan
    themselves — driving a live JSON-RPC call over ``/api/mcp/http`` —
    use ``seeded_app_fresh`` instead (see
    tests/test_mcp_oauth_handshake.py).
    """
    app, pristine_state = _shared_seeded_app
    result = _seed_users_and_mint_tokens(app)
    result["env"] = e2e_env

    yield result

    # Undo any per-test mutation of the shared app so the next test sees the
    # same object `create_app()` produced. `app.state` is backed by a plain
    # dict (starlette.datastructures.State._state) — clear + refill rather
    # than reassign so any closure that already captured `app.state` still
    # sees the restored values.
    app.state._state.clear()
    app.state._state.update(pristine_state)
    app.dependency_overrides.clear()


@pytest.fixture
def seeded_app_fresh(e2e_env):
    """Same shape as ``seeded_app`` (seeded users, four role tokens,
    TestClient) but builds its OWN fresh ``create_app()`` instead of reusing
    the session-shared one.

    For tests that must run the app's ASGI *lifespan* themselves (``with
    seeded_app_fresh["client"] as client:``) — currently only the handful in
    tests/test_mcp_oauth_handshake.py that drive a real JSON-RPC call over
    the streamable MCP mount. The SDK's ``StreamableHTTPSessionManager.run()``
    may be entered at most once per instance and cannot be restarted after
    its context exits (see ``app/api/mcp_streamable.py::
    streamable_session_manager_lifespan``), so those tests need a private
    app/session-manager instance, not the one every other ``seeded_app``
    test shares. Everything else should keep using ``seeded_app`` for the
    speed win — see ``_shared_seeded_app``.
    """
    from app.main import create_app

    app = create_app()
    result = _seed_users_and_mint_tokens(app)
    result["env"] = e2e_env
    return result


@pytest.fixture
def mock_extract_factory(e2e_env):
    """Factory fixture for creating mock extract.duckdb files.

    Returns a callable: factory(source_name, tables, remote_attach=None)
      - source_name: str — name of the connector source directory
      - tables: list[dict] — same format as create_mock_extract
      - remote_attach: list[dict] | None — rows for _remote_attach table,
        each dict with keys: alias, extension, url, token_env
    """

    def _factory(source_name: str, tables: list[dict], remote_attach=None):
        db_path = create_mock_extract(e2e_env["extracts_dir"], source_name, tables)
        if remote_attach:
            conn = duckdb.connect(str(db_path))
            conn.execute("""CREATE TABLE IF NOT EXISTS _remote_attach (
                alias VARCHAR,
                extension VARCHAR,
                url VARCHAR,
                token_env VARCHAR
            )""")
            for row in remote_attach:
                conn.execute(
                    "INSERT INTO _remote_attach VALUES (?, ?, ?, ?)",
                    [row["alias"], row["extension"], row["url"], row["token_env"]],
                )
            conn.close()
        return db_path

    return _factory


@pytest.fixture
def analyst_user(seeded_app):
    """Convenience fixture returning analyst auth headers dict."""
    token = seeded_app["analyst_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_user(seeded_app):
    """Convenience fixture returning admin auth headers dict."""
    token = seeded_app["admin_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def bq_access():
    """Build a BqAccess with pluggable factories and override the FastAPI Depends.

    Usage:
        def test_x(bq_access):
            mock_client = MagicMock()
            bq = bq_access(client=mock_client)
            # endpoint test code

    Override is auto-cleared on fixture teardown.

    NOTE: `contextlib.nullcontext(duckdb_conn)` does NOT close the conn on exit.
    The production path closes via _default_duckdb_session_factory. Tests that
    care about close behavior should use that factory directly (see
    tests/test_bq_access.py::TestDefaultDuckdbSessionFactory).
    """
    from app.main import app
    from connectors.bigquery.access import BqAccess, BqProjects, get_bq_access

    def _build(*, client=None, duckdb_conn=None, billing="test-billing", data="test-data"):
        bq = BqAccess(
            BqProjects(billing=billing, data=data),
            client_factory=(lambda projects: client) if client is not None else None,
            duckdb_session_factory=(lambda projects: _contextlib.nullcontext(duckdb_conn))
            if duckdb_conn is not None
            else None,
        )
        app.dependency_overrides[get_bq_access] = lambda: bq
        return bq

    yield _build
    from app.main import app as _app

    _app.dependency_overrides.pop(get_bq_access, None)


# ---------------------------------------------------------------------------
# Clean-bootstrap test suite (Task 20).
#
# Re-export the analyst-bootstrap fixtures so individual test modules can
# request them by name without an explicit import. Imported at module level
# so pytest collection sees the names; the fixtures themselves don't run
# until a test pulls them in.
# ---------------------------------------------------------------------------
from tests.fixtures.analyst_bootstrap import (  # noqa: E402,F401
    NONEXISTENT_TABLE,
    fastapi_test_server,
    test_pat,
    test_pat_no_grants,
    web_session,
    zero_grants_workspace,
)


@pytest.fixture
def bq_instance(monkeypatch):
    """Force instance.yaml to look like a BigQuery deployment for the
    duration of one test. Patches the cached load_instance_config so
    /admin/server-config reads / get_value('data_source.bigquery.project')
    return what we want, without touching the on-disk instance.yaml.

    Tests that need BigQuery-specific admin API behaviour (project_id
    validation, materialized source_query checks, etc.) depend on this
    fixture. Yields the fake config dict so callers can inspect it.

    Note: several test files (test_admin_bq_register.py,
    test_admin_tables_ui_materialized.py, …) define their own local
    ``bq_instance`` fixture. Those local definitions shadow this one
    inside those files — the conftest copy is the canonical provider for
    any new test file that imports from this module."""
    fake_cfg = {
        "data_source": {
            "type": "bigquery",
            "bigquery": {"project": "my-test-project", "location": "us"},
        },
    }
    monkeypatch.setattr(
        "app.instance_config.load_instance_config",
        lambda: fake_cfg,
        raising=False,
    )
    from app.instance_config import reset_cache

    reset_cache()
    yield fake_cfg
    reset_cache()


@pytest.fixture
def stub_bq_extractor(monkeypatch):
    """Mirror tests/test_admin_bq_register.py — bypasses real-BQ traffic
    in the post-register rebuild path so the test stays offline. Required
    whenever the test seeds a remote-mode BQ row via the HTTP API.

    Patches:
    - ``connectors.bigquery.extractor.rebuild_from_registry`` — returns a
      minimal success dict so the admin register endpoint's 200/201 path
      completes without touching a real BQ project.
    - ``src.orchestrator.SyncOrchestrator`` — replaced with a no-op mock so
      the post-register orchestrator.rebuild() call doesn't scan the
      (empty) extracts directory during tests.

    Returns the ``rebuild_from_registry`` MagicMock directly so callers
    that only need the side-effect patcher can ignore the return value,
    and callers that want to assert call args can inspect it."""
    rebuild_mock = MagicMock(
        return_value={
            "project_id": "my-test-project",
            "tables_registered": 1,
            "errors": [],
            "skipped": False,
        }
    )
    monkeypatch.setattr(
        "connectors.bigquery.extractor.rebuild_from_registry",
        rebuild_mock,
    )
    monkeypatch.setattr(
        "src.orchestrator.SyncOrchestrator",
        lambda *a, **kw: MagicMock(),
    )
    return rebuild_mock


def grant_table_via_package(
    conn,
    table_id: str,
    user_id: str,
    *,
    group_name: str = "analyst-pkg-grants",
    requirement: str = "required",
) -> str:
    """Test helper — wrap a single table in an auto-named data_package and
    grant the package to a custom group the user belongs to.

    Replaces the legacy "per-table resource_grants" pattern: stack-gated
    RBAC routes all analyst visibility through data_packages, so a
    standalone TABLE grant no longer surfaces the table to the analyst.
    Returns the data_package id so callers can revoke (DELETE package
    → tables_in_package + grants cascade) or assert membership.

    Defaults to ``requirement='required'`` so the wrapping package
    lands in the user's stack automatically — every existing test that
    just asserted "table visible after grant" stays correct without
    needing an explicit subscribe step.
    """
    from src.repositories.data_packages import DataPackagesRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(group_name)
    if not grp:
        grp = groups.create(
            name=group_name,
            description="test",
            created_by="test",
        )
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(
            user_id,
            grp["id"],
            source="admin",
            added_by="test",
        )

    pkgs = DataPackagesRepository(conn)
    pkg_slug = f"_test-pkg-{table_id.lower()}"[:63]
    existing = pkgs.get_by_slug(pkg_slug) if hasattr(pkgs, "get_by_slug") else None
    if existing:
        pkg_id = existing["id"]
    else:
        pkg_id = pkgs.create(
            name=f"Test wrap {table_id}",
            slug=pkg_slug,
            description=None,
            icon=None,
            color=None,
            created_by="test",
        )
    pkgs.add_table(pkg_id, table_id, added_by="test")

    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "data_package", pkg_id):
        grants.create(
            group_id=grp["id"],
            resource_type="data_package",
            resource_id=pkg_id,
            assigned_by="test",
            requirement=requirement,
        )
    return pkg_id


def revoke_table_via_package(conn, table_id: str) -> None:
    """Mirror of :func:`grant_table_via_package` — drops the wrapping
    data_packages (and via FK cascade the junction + grants) for every
    auto-package that wraps this table.
    """
    rows = conn.execute(
        "SELECT DISTINCT package_id FROM data_package_tables WHERE table_id = ?",
        [table_id],
    ).fetchall()
    for r in rows:
        # Hard-delete via raw SQL so the test fixture doesn't leak rows
        # across tests sharing the seeded_app DB.
        conn.execute(
            "DELETE FROM resource_grants WHERE resource_type = 'data_package' AND resource_id = ?",
            [r[0]],
        )
        conn.execute(
            "DELETE FROM data_package_tables WHERE package_id = ?",
            [r[0]],
        )
        conn.execute("DELETE FROM data_packages WHERE id = ?", [r[0]])


@pytest.fixture(autouse=True)
def _deterministic_mcp_url_resolver(monkeypatch):
    """No test may depend on this machine's DNS.

    The MCP source-url guard (#1154) resolves a hostname on every source
    create/repoint, so a dozen API-level suites began making a real
    `getaddrinfo` call per request. That is slow where the resolver is slow,
    and WRONG where it hijacks NXDOMAIN: a resolver that answers reserved
    names with a parking IP silently flips every "does not resolve"
    assertion, and parks a default-executor thread while it does it
    (Devin on #1204).

    Reserved names (RFC 2606 / 6761 — `.example`, `.invalid`, `.test`, and
    the `example.com|net|org` subdomains) raise, which is what a conforming
    resolver does; `localhost` is loopback; anything else is public. Suites
    that need a particular answer keep passing `_resolver=` straight to
    `check_source_url`, which this fixture does not touch.
    """
    import socket as _socket

    import src.net.mcp_source_url as _m

    _RESERVED_SUFFIXES = (
        ".example",
        ".invalid",
        ".test",
        ".example.com",
        ".example.net",
        ".example.org",
    )

    def _resolver(host: str):
        h = (host or "").lower().rstrip(".")
        if h == "localhost" or h.endswith(".localhost"):
            return ["127.0.0.1"]
        if h.endswith(_RESERVED_SUFFIXES):
            raise _socket.gaierror(f"Name or service not known: {host}")
        return ["93.184.216.34"]

    monkeypatch.setattr(_m, "_default_resolver", _resolver)
