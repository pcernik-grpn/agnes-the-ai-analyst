"""Windows-only deferred self-update helper (spawned detached by
``cli/commands/self_upgrade.py``).

On Windows a running executable image and its loaded ``.dll`` / ``.pyd`` files
are held under a mandatory lock, so ``uv tool install --force`` cannot replace
the very venv the running agnes lives in — it fails with ``os error 5`` (Access
is denied) while removing the ``Scripts`` dir, and a half-done in-place swap
CORRUPTS the install. POSIX lets you unlink a running binary; Windows does not.

This helper is the VS-Code-style fix: it runs OUTSIDE the agnes tool venv
(launched by a NON-target Python interpreter, from a temp copy of this file so
it holds no handle inside the venv), waits for the agnes process that spawned it
to exit (releasing the lock), then performs ``uv tool install --force``, verifies
the new binary, and rolls back to the last-known-good cached wheel on failure.
The outcome is written to ``upgrade_status.json`` so it surfaces on the next
non-quiet agnes command.

Two Windows realities this handles beyond the swap itself:
1. HEADLESS — every child process is spawned ``CREATE_NO_WINDOW``. The helper
   has no console, so a console child would otherwise get a fresh one allocated,
   flashing a window per ``tasklist`` poll / ``uv`` retry / verify. (What the
   operator first saw as "blinking".)
2. STATUSLINE CONTENTION — the tool venv is re-locked on every ``agnes
   statusline`` render, which can beat a naive retry. So the install is gated on
   a best-effort "is the venv free" probe (attempt in the gap between renders,
   not against a guaranteed lock) and retried patiently, and for the duration of
   the swap a ``deferred-update.active`` sentinel tells the statusline to step
   aside.
3. APPLICATION CONTROL — Smart App Control / WDAC can refuse the unsigned
   artifacts of a uv tool install (the generated ``agnes.exe`` trampoline, the
   downloaded interpreter) with ``os error 4551``. Unlike a lock it never
   clears, so it is classified first, never retried, and named in the recorded
   outcome — this console-less helper's only channel to the human (#2342).

Uses ONLY the standard library plus the ``uv`` executable on PATH — it never
imports ``cli``, so it holds no lock on the tool venv it is replacing.

Invocation (all args positional; rollback_wheel optional / may be empty):

    <non-target-python> _win_deferred_update.py <parent_pid> <staged_wheel>
        <expected_version> <config_dir> [<rollback_wheel>]
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

_WAIT_TIMEOUT_S = 120.0      # how long to wait for the agnes process to exit
_INSTALL_BUDGET_S = 300.0    # keep retrying while the venv is locked, up to ~5 min
_INSTALL_BACKOFF_S = 2.0

# Windows: run every child console program WITHOUT popping a console window.
# The helper itself is spawned CREATE_NO_WINDOW, but a windowless parent that
# launches a console child makes Windows allocate a FRESH console for that child
# — that is the flashing the operator saw (`tasklist` polled ~1/s in the wait
# loop, `uv` retried, the verify `agnes --version`). Propagating CREATE_NO_WINDOW
# to every child keeps the whole deferred update headless. The flag exists only
# on Windows; it is 0 elsewhere so this module's unit tests still run on POSIX.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# uv-tool package name — used to locate the venv python we probe for "is the
# tool venv currently in use" before each install attempt.
_TOOL_PKG = "agnes-the-ai-analyst"

# While swapping the venv the helper drops this sentinel in the config dir;
# `agnes statusline` sees it and steps aside so the status bar isn't relaunching
# the very venv `uv tool install --force` is trying to replace on every render.
_UPDATING_SENTINEL = "deferred-update.active"


def _log(config_dir: str, msg: str) -> None:
    try:
        p = os.path.join(config_dir, "deferred-update.log")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` is still running (Windows ``tasklist``)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return str(pid) in (out.stdout or "")


def _wait_for_exit(pid: int, *, timeout_s: float = _WAIT_TIMEOUT_S,
                   interval_s: float = 1.0) -> None:
    """Block until ``pid`` exits or the timeout elapses (best-effort — we
    proceed either way; the retry loop absorbs a still-held lock)."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if not _pid_alive(pid):
            return
        time.sleep(interval_s)


def _venv_python() -> "str | None":
    """Path to the tool venv's ``python.exe`` — the file the swap must replace,
    used only as a best-effort "is the venv in use right now" probe. ``None`` if
    uv can't tell us where the tool dir is (probe then degrades to "attempt")."""
    try:
        out = subprocess.run(
            ["uv", "tool", "dir"], capture_output=True, text=True,
            timeout=10, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return os.path.join(out.stdout.strip(), _TOOL_PKG, "Scripts", "python.exe")


def _venv_free(py_path: "str | None") -> bool:
    """Best-effort: ``True`` when the venv ``python.exe`` is NOT currently running
    — a running image is locked against write/delete on Windows, so opening it
    ``r+b`` raises ``PermissionError`` iff some agnes process (a statusline
    render, another session) holds it. Opening for write shares read, so it never
    blocks a concurrent launch, and we write nothing. Unknown/undiscoverable →
    ``True`` (attempt anyway; a locked attempt fails cleanly and we just retry)."""
    if not py_path or not os.path.exists(py_path):
        return True
    try:
        with open(py_path, "r+b"):
            return True
    except OSError:
        return False


# Substrings in uv's stderr that mean "the tool venv is locked by another
# process right now" (a concurrent agnes/statusline holding the running
# python.exe). ONLY these are worth waiting out; any other uv failure won't fix
# itself, so we fail fast with the real message instead of burning the whole
# budget mislabeled as "locked" (which is what hid a bad staged-wheel filename).
_LOCK_HINTS = ("os error 5", "access is denied", "being used", " in use", "denied")


def _looks_like_lock(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in _LOCK_HINTS)


# Windows application control (Smart App Control / WDAC) refusing a binary it
# cannot verify, at LOAD time — `os error 4551`. Both artifacts of a uv tool
# install are unsigned: the generated `agnes.exe` trampoline and the
# interpreter uv downloads (#2342).
#
# Stdlib-only twin of `cli.win_app_control.is_app_control_block` /
# `block_reason`. That module cannot be imported here: this file runs from a
# temp copy OUTSIDE the tool venv and must hold no handle inside it (see the
# module docstring). `tests/test_win_app_control.py` pins both the marker
# tuple and the reason wording to the original.
_APP_CONTROL_WINERROR = 4551
_APP_CONTROL_MARKERS = ("error 4551", "errno 4551",
                        "application control policy has blocked")
_APP_CONTROL_DOC = 'docs/QUICKSTART.md → "Windows: Smart App Control blocks the CLI"'


def _looks_like_app_control(failure: object) -> bool:
    """True iff ``failure`` (an exception from a spawn, or another process's
    captured stderr) is an application-control block."""
    if failure is None:
        return False
    if getattr(failure, "winerror", None) == _APP_CONTROL_WINERROR:
        return True
    t = str(failure).lower()
    return any(m in t for m in _APP_CONTROL_MARKERS)


def _app_control_reason(binary: str | None = None) -> str:
    """One short line for ``upgrade_status.json`` (capped at 200 chars there),
    naming the block so the next non-quiet agnes command can explain it."""
    what = binary.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] if binary else ""
    return (f"windows application control blocked {what or 'the agnes binary'} "
            f"(os error 4551) — Smart App Control refuses unsigned binaries; "
            f"see {_APP_CONTROL_DOC}")


def _uv_install(wheel: str, *, config_dir: "str | None" = None,
                budget_s: float = _INSTALL_BUDGET_S,
                backoff_s: float = _INSTALL_BACKOFF_S) -> tuple[int, str]:
    """``uv tool install --force <wheel>`` (headless). Returns ``(0, "")`` on
    success, else ``(last rc, one-line reason)`` — the reason travels back to
    `run` so the recorded outcome can say WHAT failed, not just that it did.

    Retries ONLY while the failure looks like a Windows file lock — the venv
    being replaced is briefly held by a concurrent agnes process (a statusline
    render / another session). To land an attempt in the gap between renders
    rather than hammer `uv` against a guaranteed lock, it attempts only when the
    venv python looks free (near the deadline it attempts regardless). ANY other
    error (a bad wheel filename, uv missing, an application-control policy
    refusing the unsigned artifacts) FAILS FAST with the real stderr logged —
    retrying it would just waste the budget and, historically, get mislabeled
    as 'venv locked'."""
    py = _venv_python()
    deadline = time.monotonic() + max(0.0, budget_s)
    rc, err = 1, ""
    while True:
        near_deadline = time.monotonic() >= deadline - backoff_s
        if _venv_free(py) or near_deadline:
            try:
                p = subprocess.run(
                    ["uv", "tool", "install", "--force", wheel],
                    capture_output=True, text=True, creationflags=_NO_WINDOW,
                )
                rc, err = p.returncode, (p.stderr or "").strip()
            except OSError as e:
                rc, err = 1, str(e)
            if rc == 0:
                return 0, ""
            # Checked BEFORE the lock hints: an application-control message can
            # carry lock-ish words, and burning the 5-minute budget on a policy
            # that never clears would bury the real cause.
            if _looks_like_app_control(err):
                if config_dir:
                    _log(config_dir, f"uv install blocked by application control rc={rc}: {err[:300]}")
                return rc, _app_control_reason()
            if not _looks_like_lock(err):
                if config_dir:
                    _log(config_dir, f"uv install failed rc={rc}: {err[:300]}")
                # not a lock — it won't fix itself; don't retry
                return rc, f"windows deferred install failed rc={rc}"
        if time.monotonic() >= deadline:
            if config_dir:
                _log(config_dir,
                     f"uv install gave up rc={rc} after ~{int(budget_s)}s (venv locked): {err[:200]}")
            return rc, f"windows deferred install failed rc={rc} (venv locked)"
        # Jittered backoff so we don't beat in lockstep with a ~1 Hz statusline.
        time.sleep(backoff_s + (time.monotonic() % 0.7))


def _installed_version_ok(expected_version: str) -> tuple[bool, str]:
    """Run the freshly-installed agnes and confirm it boots and reports the
    expected version. Returns ``(ok, detail)`` — the same shape as
    ``cli.commands.self_upgrade._smoke_test_new_binary``, so a verify failure
    can name itself in the recorded outcome. Resolves the binary at the uv tool
    bin dir (not via PATH) when possible, and sets the recursion sentinel so
    its own update check is inert."""
    binp = ""
    try:
        out = subprocess.run(["uv", "tool", "dir", "--bin"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=_NO_WINDOW)
        if out.returncode == 0:
            binp = out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        binp = ""
    exe = os.path.join(binp, "agnes.exe") if binp else "agnes"
    env = {**os.environ, "AGNES_NO_UPDATE_CHECK": "1",
           "AGNES_SELF_UPGRADE_IN_PROGRESS": "1"}
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           timeout=30, env=env, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        # An application-control policy refusing the just-installed, unsigned
        # trampoline at LOAD lands here (`winerror == 4551`). The block is
        # invisible to the process it kills, so this spawn — from a process
        # that IS running — is the only place the deferred update can see it.
        if _looks_like_app_control(e):
            return False, _app_control_reason(exe)
        return False, f"verify spawn failed: {type(e).__name__}"
    if r.returncode != 0 and _looks_like_app_control(r.stderr or ""):
        # The trampoline started but the interpreter it loads was refused.
        return False, _app_control_reason(exe)
    # Exact match on the version TOKEN, not a substring: `expected in stdout`
    # would let "0.72.9" pass against a binary reporting "0.72.90" (or any
    # superstring), so a failed/partial swap could be scored as success and
    # skip rollback. `agnes --version` prints "agnes <version>", so the last
    # whitespace-delimited token is the version — mirrors the Mac smoke test
    # (`_smoke_test_new_binary`), minus the `packaging` dep this file forbids.
    tokens = (r.stdout or "").split()
    actual = tokens[-1] if tokens else ""
    if r.returncode == 0 and actual == expected_version:
        return True, actual
    return False, (f"verify reported '{actual}' rc={r.returncode}, "
                   f"expected {expected_version}")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_status(config_dir: str, *, success: bool, reason: str | None = None) -> None:
    """Mirror ``cli.upgrade_status.record_outcome`` from outside the venv:
    reset the failure counter on success, increment + record reason on failure."""
    p = os.path.join(config_dir, "upgrade_status.json")
    prior_failures = 0
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                prior = json.load(fh)
            v = prior.get("consecutive_failures", 0)
            if isinstance(v, int) and v >= 0:
                prior_failures = v
    except (OSError, json.JSONDecodeError):
        prior_failures = 0
    entry = {
        "last_attempt_ts": time.time(),
        "last_outcome": "success" if success else "failure",
        "consecutive_failures": 0 if success else prior_failures + 1,
    }
    if not success and reason:
        entry["last_failure_reason"] = reason[:200]
    try:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(entry, fh)
    except OSError:
        pass


def _record_last_known_good(config_dir: str, wheel: str, version: str) -> None:
    """Record the just-installed (verified) wheel as the rollback artifact for
    the NEXT upgrade, matching the schema `cli.commands.self_upgrade` reads."""
    try:
        meta = {
            "version": version,
            "wheel_filename": os.path.basename(wheel),
            "sha256": _sha256(wheel),
        }
        with open(os.path.join(config_dir, "last_known_good.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(meta, fh)
    except OSError:
        pass


def _set_updating(config_dir: str) -> None:
    """Drop the "swap in progress" sentinel so `agnes statusline` steps aside."""
    try:
        with open(os.path.join(config_dir, _UPDATING_SENTINEL), "w",
                  encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S"))
    except OSError:
        pass


def _clear_updating(config_dir: str) -> None:
    """Remove the sentinel. A crash instead leaves a stale one, which the
    statusline ignores past its TTL — so the status bar can never be wedged."""
    try:
        os.remove(os.path.join(config_dir, _UPDATING_SENTINEL))
    except OSError:
        pass


def run(parent_pid: int, staged_wheel: str, expected_version: str,
        config_dir: str, rollback_wheel: str | None = None) -> int:
    """Full deferred-update flow. Returns process exit code (0 success)."""
    _log(config_dir, f"deferred update start: pid={parent_pid} wheel={staged_wheel} "
                     f"expect={expected_version} rollback={rollback_wheel or '-'}")
    _wait_for_exit(parent_pid)

    # Ask short agnes commands (the statusline) to step aside for the swap, so
    # they aren't re-locking the venv `uv` is replacing on every render. Cleared
    # in `finally` — a crash leaves only a stale sentinel, ignored past its TTL.
    _set_updating(config_dir)
    try:
        rc, detail = _uv_install(staged_wheel, config_dir=config_dir)
        if rc != 0:
            _log(config_dir, f"install failed rc={rc} (see reason above)")
            _write_status(config_dir, success=False,
                         reason=detail or f"windows deferred install failed rc={rc}")
            return 2

        ok, detail = _installed_version_ok(expected_version)
        if ok:
            _record_last_known_good(config_dir, staged_wheel, expected_version)
            _write_status(config_dir, success=True)
            _log(config_dir, f"SUCCESS: installed {expected_version}")
            return 0

        # Verify failed → roll back to the last-known-good cached wheel if we have one.
        # An application-control block is recorded verbatim: the rollback
        # reinstalls an equally unsigned trampoline, so the reason the human
        # eventually reads must name the policy, not the version.
        _log(config_dir, f"verify failed after install: {detail}")
        if rollback_wheel and os.path.exists(rollback_wheel):
            rb, _ = _uv_install(rollback_wheel, config_dir=config_dir)
            _log(config_dir, f"rollback to {rollback_wheel} rc={rb}")
        else:
            _log(config_dir, "no rollback wheel available; leaving as-is")
        _write_status(config_dir, success=False,
                     reason=detail or f"windows deferred: smoke failed for {expected_version}")
        return 1
    finally:
        _clear_updating(config_dir)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4:
        return 64  # usage error
    parent_pid = int(args[0])
    staged_wheel = args[1]
    expected_version = args[2]
    config_dir = args[3]
    rollback_wheel = args[4] if len(args) > 4 and args[4] else None
    return run(parent_pid, staged_wheel, expected_version, config_dir, rollback_wheel)


if __name__ == "__main__":
    raise SystemExit(main())
