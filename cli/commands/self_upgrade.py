"""`agnes self-upgrade` — pull the wheel from the server, reinstall, smoke-test,
roll back on failure."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urlparse

import typer

from cli.config import _config_dir, get_server_url
from cli.lib.hooks import maybe_refresh_claude_hooks
from cli.update_check import UpdateInfo, check, format_outdated_notice
from cli.upgrade_status import record_outcome
from cli.win_app_control import block_reason, is_app_control_block
from cli.win_app_control import hint as app_control_hint

self_upgrade_app = typer.Typer(
    name="self-upgrade",
    help="Reinstall the CLI from the server's currently-shipped wheel.",
    invoke_without_command=True,
)

_SENTINEL_ENV = "AGNES_SELF_UPGRADE_IN_PROGRESS"

# `_do_install_with_smoke_and_rollback` return codes. 0/1 are the usual
# success/failure exit codes; DEFERRED means "we intentionally did NOT attempt
# an install this run" (unattended run with no safe rollback artifact) — the
# caller must treat it as a no-op: exit 0 and DO NOT touch the failure counter.
_INSTALL_OK = 0
_INSTALL_FAIL = 1
_INSTALL_DEFERRED = 2
# Windows: the swap was handed to a detached helper that completes after this
# process exits (a running .exe/.dll can't be replaced in place on Windows).
# Like DEFERRED, the caller treats it as a benign no-op (exit 0, no failure
# recorded here) — the helper records the real outcome to upgrade_status.json.
_INSTALL_STAGED = 3


class _Unreachable:
    """Sentinel returned by _resolve_info when --force was specified but the
    server probe failed. Distinguishes 'explicitly requested an upgrade and
    we couldn't reach the server' (exit 1, stderr) from 'no upgrade needed'
    (exit 0, silent)."""


_UNREACHABLE = _Unreachable()


class _Offline:
    """Sentinel returned by _resolve_info when --force was NOT specified and
    the server probe failed. Distinguishes 'couldn't check, take no opinion'
    (exit 0, silent, don't touch failure counter) from 'CLI is current' —
    so a transient network blip during the SessionStart hook does not reset
    the consecutive-failure count an analyst has accumulated from real
    install failures (Devin BUG_0001 on #601)."""


_OFFLINE = _Offline()


class _Redirected:
    """Sentinel for "the server answered, with a redirect the CLI won't follow".

    Distinct from ``_Unreachable``/``_Offline`` because the probe did NOT
    fail: a relocated deployment leaves the old hostname answering `308`,
    and `_fetch_latest` swallows that into `None` (its `raise_for_status()`
    ignores 3xx, then `.json()` trips on the redirect's empty body). Mapped
    to "probe failed", that produced a silent no-op without `--force` and
    the untrue `cannot reach <old>/cli/latest` with it — on the very
    command someone runs to repair a stale install.

    Covers EVERY redirect, not only a cross-host move: a same-origin bounce
    (an SSO proxy answering `302 /login`) or a downgrade to plaintext is
    equally a probe the CLI cannot complete, and "cannot reach" is equally
    untrue of it — the server answered. What differs is the remedy, and
    ``cli.server_moved`` already classifies that, handing back the new
    address for a real move and a proxy hint otherwise. Named for the
    evidence (a redirect), not the diagnosis (Devin Review on #1275).
    """

    def __init__(self, message: str, reason: str) -> None:
        self.message = message
        #: One short line for ``upgrade_status.json`` — the multi-line
        #: ``message`` is for a terminal, this rides inside a warning
        #: sentence on some later command.
        self.reason = reason


def _invalidate_update_cache() -> None:
    """Drop update_check.json so the next CLI invocation re-probes /cli/latest."""
    (_config_dir() / "update_check.json").unlink(missing_ok=True)


def _last_known_good_path() -> Path:
    return _config_dir() / "last_known_good.json"


def _read_last_known_good_meta() -> dict:
    """Full last-known-good record: ``{download_url, version, wheel_filename,
    sha256}`` (any subset). ``{}`` on missing/malformed. The rollback source of
    truth — ``wheel_filename``+``sha256`` point at a locally-cached wheel."""
    p = _last_known_good_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_last_known_good() -> Optional[str]:
    """Back-compat shim: the prior wheel's download URL (or None). Kept so old
    callers/tests that only need the URL keep working; the full record is
    ``_read_last_known_good_meta``."""
    return _read_last_known_good_meta().get("download_url")


def _record_last_known_good_meta(meta: dict) -> None:
    p = _last_known_good_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(meta), encoding="utf-8")
    except OSError:
        pass  # best-effort — failure to record must not break the flow


def _record_last_known_good(download_url: str) -> None:
    """Back-compat: record only the download URL. Superseded by
    ``_record_last_known_good_meta`` (which also records the cached wheel
    filename + sha256); retained so older callers/tests keep working."""
    _record_last_known_good_meta({"download_url": download_url})


# --------------------------------------------------------------------------- #
# Local wheel cache — the rollback artifact source (FIX 3). The server serves
# only the latest wheel (older filenames 404), so a URL-based rollback is dead
# once a newer wheel ships. Instead we keep the last N verified wheels on disk
# and roll back from there, sha256-verified.
# --------------------------------------------------------------------------- #
_WHEEL_CACHE_KEEP = 2


def _wheel_cache_dir() -> Path:
    return _config_dir() / "wheels"


def _sha256_file(p: Path) -> str:
    """Streamed sha256 hex digest (don't load the whole wheel into memory)."""
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _sanitize_version(v: str) -> str:
    """Filesystem-safe wheel-cache filename stem from a version string."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", v or "unknown")


def _gc_wheel_cache(keep_name: str) -> None:
    """Keep only the newest ``_WHEEL_CACHE_KEEP`` wheels (plus ``keep_name``);
    delete the rest. Best-effort — a locked/undeletable file is harmless."""
    try:
        cache = _wheel_cache_dir()
        wheels = sorted(cache.glob("*.whl"), key=lambda p: p.stat().st_mtime, reverse=True)
        kept = 0
        for w in wheels:
            if w.name == keep_name:
                continue
            kept += 1
            if kept >= _WHEEL_CACHE_KEEP:
                w.unlink(missing_ok=True)
    except OSError:
        pass


def _record_wheel_cache(version: str, wheel_path: Path, download_url: Optional[str] = None) -> dict:
    """Copy a verified wheel into the cache, GC to the last N, and return the
    ``{version, wheel_filename, sha256}`` metadata. ``{}`` on any error (the
    caller still records the download URL — the cache is a best-effort rollback
    aid, not a correctness dependency).

    The cached file MUST keep a PEP 427-valid name (``name-version-tags.whl``):
    rollback reinstalls it via ``uv tool install --force <path>``, and uv parses
    the FILENAME and rejects a bare ``<version>.whl`` with "Must have a version"
    — so a bare name here silently breaks rollback (the same trap the Windows
    deferred staging hit; see ``_staged_wheel_name``)."""
    try:
        cache = _wheel_cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        fname = _staged_wheel_name(download_url, version)
        dest = cache / fname
        # Skip the copy when the wheel is already staged at the destination
        # (the POSIX staged-first path lands the file here directly).
        if wheel_path.resolve() != dest.resolve():
            shutil.copyfile(wheel_path, dest)
        sha = _sha256_file(dest)
        _gc_wheel_cache(keep_name=fname)
        return {"version": version, "wheel_filename": fname, "sha256": sha}
    except OSError:
        return {}


def _cached_wheel_for(meta: dict) -> Optional[Path]:
    """Return the cached wheel Path iff it exists AND its sha256 matches the
    recorded digest (defends against a truncated/corrupt cache). Else None."""
    if not meta:
        return None
    fname = meta.get("wheel_filename")
    sha = meta.get("sha256")
    if not fname or not sha:
        return None
    p = _wheel_cache_dir() / fname
    if not p.exists():
        return None
    try:
        return p if _sha256_file(p) == sha else None
    except OSError:
        return None


def _rollback_artifact_ok(meta: dict) -> bool:
    """True iff ``meta`` names a rollback wheel AND that wheel is present +
    sha256-valid in the cache. False when there is no prior at all (first-ever
    upgrade) — the caller distinguishes 'no prior' from 'prior expected but
    missing' via ``meta`` being empty vs. carrying a ``wheel_filename``."""
    return _cached_wheel_for(meta) is not None


def _uv_tool_bin_path() -> Optional[Path]:
    """Locate the agnes shim uv installed.

    Tries `uv tool dir --bin` (uv >= 0.5). Falls back to uv's documented
    default install location on older uv where `--bin` is rejected.
    """
    bin_dir: Optional[Path] = None
    try:
        out = subprocess.run(
            ["uv", "tool", "dir", "--bin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            bin_dir = Path(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        bin_dir = None

    if bin_dir is None:
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA")
            if appdata:
                bin_dir = Path(appdata) / "uv" / "tools" / "bin"
        else:
            bin_dir = Path.home() / ".local" / "bin"

    if bin_dir is None or not bin_dir.exists():
        return None

    for name in ("agnes.exe", "agnes"):
        candidate = bin_dir / name
        if candidate.exists():
            return candidate
    return None


def _pip_bin_path(user: bool = False) -> Optional[Path]:
    """Console-script path for a pip install.

    Normally `<venv>/bin/agnes` (POSIX) or `<venv>\\Scripts\\agnes.exe`
    (Windows), resolved next to ``sys.executable``. For a ``pip install --user``
    the console script lands under the per-user base (``site.getuserbase()``),
    NOT next to ``sys.executable`` — so check there first when ``user`` is set,
    or the smoke test can't find the binary and false-fails into a rollback.
    """
    name = "agnes.exe" if sys.platform == "win32" else "agnes"
    subdir = "Scripts" if sys.platform == "win32" else "bin"
    candidates = []
    if user:
        try:
            base = site.getuserbase()
        except Exception:
            base = None
        if base:
            candidates.append(Path(base) / subdir / name)
    candidates.append(Path(sys.executable).parent / name)
    for c in candidates:
        if c.exists():
            return c
    return None


def _canonical(p: Path) -> str:
    """Canonical, case-normalized string form of a path for containment checks.

    ``os.path.realpath`` resolves symlinks in the path's *directory* prefix;
    ``os.path.normcase`` folds case on case-insensitive filesystems (Windows,
    macOS HFS+). Apply ONLY to directories (the venv prefix, the uv tool dir),
    never to the interpreter symlink — resolving *that* is exactly the bug this
    module avoids (see ``_python_is_uv_tool_install``).
    """
    return os.path.normcase(os.path.realpath(str(p)))


def _path_is_within(child: Path, parent: Path) -> bool:
    """True iff ``child`` equals ``parent`` or is nested under it.

    Compares canonicalized + case-normalized paths via ``os.path.commonpath``
    (component-aware — ``/a/bc`` is NOT within ``/a/b``, unlike ``startswith``).
    Returns False rather than raising when the two live on different roots or
    Windows drives (``commonpath`` raises ``ValueError`` there).
    """
    c = _canonical(child)
    p = _canonical(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:
        return False


def _python_is_uv_tool_install() -> bool:
    """True iff the RUNNING interpreter is a uv-managed tool install.

    Routing key for self-upgrade. ``uv tool install --force`` only rewrites the
    venv uv itself manages (``<uv tool dir>/<pkg>/``); if the running agnes was
    installed elsewhere (project venv via ``pip install -e .``, pipx, system
    pip, …) the uv install would land in a different binary while the active one
    stays stale. When this returns False, route through pip targeting
    ``sys.executable`` so the actually-running binary is upgraded.

    Anchored on ``sys.prefix`` (the venv directory), NOT ``sys.executable``. A
    venv's ``bin/python`` is a symlink to the base interpreter, so
    ``Path(sys.executable).resolve()`` follows it OUT of the uv tree (e.g. into
    the Homebrew Cellar) and the containment check wrongly fails — the bug that
    routed every uv-tool self-upgrade to pip, where a uv venv has no pip
    (``No module named pip``). ``sys.prefix`` is the real venv dir and is not a
    symlink to elsewhere, so it stays inside the uv tree.
    """
    if not shutil.which("uv"):
        return False
    try:
        out = subprocess.run(
            ["uv", "tool", "dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return False
        uv_tool_root = Path(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return _path_is_within(Path(sys.prefix), uv_tool_root)


def _is_editable_install() -> bool:
    """True iff agnes is an editable (``pip install -e .``) checkout.

    Reads the distribution's PEP 610 ``direct_url.json``; an editable install
    carries ``dir_info.editable == true``. Best-effort — any error (metadata
    absent, not installed) returns False so a classification probe never
    crashes the upgrade path.
    """
    try:
        import importlib.metadata as md

        raw = md.distribution("agnes-the-ai-analyst").read_text("direct_url.json")
        if not raw:
            return False
        return bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except Exception:
        return False


def _in_pipx_venv() -> bool:
    """True iff the running interpreter lives inside a pipx-managed venv
    (``$PIPX_HOME/venvs/`` or the default ``~/.local/pipx/venvs/``)."""
    roots = []
    pipx_home = os.environ.get("PIPX_HOME")
    if pipx_home:
        roots.append(Path(pipx_home) / "venvs")
    roots.append(Path.home() / ".local" / "pipx" / "venvs")
    return any(_path_is_within(Path(sys.prefix), r) for r in roots)


def _in_user_site() -> bool:
    """True iff the agnes package is installed under the per-user site
    (``pip install --user``). Best-effort; False on any probe error."""
    try:
        user_site = site.getusersitepackages()
    except Exception:
        return False
    if not user_site:
        return False
    try:
        import importlib.metadata as md

        loc = md.distribution("agnes-the-ai-analyst").locate_file("")
        return _path_is_within(Path(str(loc)), Path(user_site))
    except Exception:
        return False


def _classify_install_method() -> tuple[str, dict]:
    """Classify how the running agnes was installed → ``(method, ctx)``.

    ``method`` ∈ {"editable", "uv-tool", "pipx", "venv", "user", "system"}.
    Evaluated top-to-bottom, first match wins — the order disambiguates the
    overlaps: an editable install can also be inside a venv; a uv-tool venv is
    also a venv; a pipx venv is also a venv. ``ctx`` carries method-specific
    data (currently ``{"user": True}`` for the ``--user`` console-script lookup).
    """
    if _is_editable_install():
        return "editable", {}
    if _python_is_uv_tool_install():
        return "uv-tool", {}
    if _in_pipx_venv():
        return "pipx", {}
    if sys.prefix != sys.base_prefix:
        return "venv", {}
    if _in_user_site():
        return "user", {"user": True}
    return "system", {}


def _install_with_uv(download_url: str, *, quiet: bool) -> int:
    out = subprocess.DEVNULL if quiet else None
    return subprocess.run(["uv", "tool", "install", "--force", download_url], stdout=out).returncode


def _install_with_pip(download_url: str, *, quiet: bool, user: bool = False) -> int:
    """Install into the SAME interpreter that's running this command.

    ``sys.executable`` owns the live `agnes` binary; `python3` would
    PATH-resolve to a different (system) interpreter on macOS. ``--user`` is
    passed ONLY for a genuine ``pip install --user`` (method == "user") — it is
    wrong inside a venv/uv-tool (targets ~/.local outside the venv). Deps are
    resolved (NO ``--no-deps``) so a release that bumps a dependency doesn't
    leave a stale transitive pinned to the previous version.
    """
    out = subprocess.DEVNULL if quiet else None
    with tempfile.TemporaryDirectory(prefix="agnes_cli.") as td:
        wheel_path = Path(td) / "agnes.whl"
        rc = subprocess.run(["curl", "-fsSL", "-o", str(wheel_path), download_url], stdout=out).returncode
        if rc != 0:
            return rc
        cmd = [sys.executable, "-m", "pip", "install", "--force-reinstall"]
        if user:
            cmd.append("--user")
        cmd.append(str(wheel_path))
        return subprocess.run(cmd, stdout=out).returncode


def _download_wheel(url: str, dest_dir: Path, *, quiet: bool) -> Optional[Path]:
    """Download the wheel to ``dest_dir`` via curl. Returns the local path (on
    curl exit 0) or None. Used to obtain a local copy for the wheel cache after
    a successful install; the return is rc-based (not existence-checked) so it
    stays mockable in tests that stub ``subprocess.run``."""
    out = subprocess.DEVNULL if quiet else None
    wheel_path = dest_dir / "agnes.whl"
    rc = subprocess.run(["curl", "-fsSL", "-o", str(wheel_path), url], stdout=out).returncode
    return wheel_path if rc == 0 else None


def _install_local_wheel(wheel: Path, *, method: str, quiet: bool, user: bool) -> int:
    """Install an already-downloaded LOCAL wheel (rollback-from-cache path).

    ``method`` is "uv" or "pip". Unlike ``_install_with_pip`` (which curls a
    URL), this installs the local file directly — no network fetch for the
    artifact, so a rollback works even after the server has rotated its wheel."""
    out = subprocess.DEVNULL if quiet else None
    if method == "uv":
        return subprocess.run(["uv", "tool", "install", "--force", str(wheel)], stdout=out).returncode
    cmd = [sys.executable, "-m", "pip", "install", "--force-reinstall"]
    if user:
        cmd.append("--user")
    cmd.append(str(wheel))
    return subprocess.run(cmd, stdout=out).returncode


def _helper_interpreter() -> Optional[str]:
    """A Python interpreter OUTSIDE the agnes tool venv, to run the Windows
    deferred-update helper. Using the target venv's own python would re-create
    the self-lock the helper exists to avoid. ``sys.base_prefix`` is the base
    interpreter the venv was built from (a uv-managed or system python in a
    different tree). Returns its path, or None if none is usable (the caller
    then fails safe rather than attempting the corrupting in-place swap)."""
    exe = "python.exe" if sys.platform == "win32" else "python"
    candidate = Path(sys.base_prefix) / exe
    if candidate.exists() and not _path_is_within(candidate, Path(sys.prefix)):
        return str(candidate)
    # Fallback: search PATH for a python outside the running venv.
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found and not _path_is_within(Path(found), Path(sys.prefix)):
            return found
    return None


def _staged_wheel_name(download_url: Optional[str], version: Optional[str]) -> str:
    """A PEP 427-valid wheel filename for the locally-staged wheel.

    ``uv tool install <file>`` parses the FILENAME (``name-version-<tags>.whl``)
    and rejects a bare ``<version>.whl`` with "Must have a version" — so the
    Windows deferred helper (which installs the staged wheel by path) must keep
    the server's real wheel filename, e.g.
    ``agnes_the_ai_analyst-0.72.4-py3-none-any.whl``. The server's
    ``download_url`` already ends in that name; fall back to a constructed valid
    name if it's somehow missing/degenerate."""
    name = os.path.basename(urlparse(download_url or "").path)
    if name.endswith(".whl") and "-" in name:
        return name
    return f"agnes_the_ai_analyst-{_sanitize_version(version or 'unknown')}-py3-none-any.whl"


def _spawn_windows_deferred_update(info: UpdateInfo, prior_meta: dict, *, quiet: bool) -> bool:
    """Stage the new wheel and spawn a DETACHED helper that installs it after
    this agnes process exits (Windows can't replace its own running files in
    place). Returns True if the helper was spawned; False if we couldn't stage
    safely — the caller then fails safe and never attempts the corrupting
    in-place swap."""
    if not shutil.which("uv"):
        return False
    py = _helper_interpreter()
    if py is None:
        return False
    # Stage the NEW wheel into the cache dir so the detached helper (which runs
    # after we've exited) has a local file to install from.
    cache = _wheel_cache_dir()
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    staged = cache / _staged_wheel_name(info.download_url, info.latest)
    with tempfile.TemporaryDirectory(prefix="agnes_stage.") as td:
        dl = _download_wheel(info.download_url, Path(td), quiet=quiet)
        if dl is None:
            return False
        try:
            shutil.copyfile(dl, staged)
        except OSError:
            return False
    # Copy the helper OUT of the venv before spawning: uv will wipe the venv,
    # so a handle inside it would block removal / vanish mid-run.
    src = Path(__file__).with_name("_win_deferred_update.py")
    helper = Path(tempfile.gettempdir()) / f"agnes_deferred_update_{os.getpid()}.py"
    try:
        shutil.copyfile(src, helper)
    except OSError:
        return False
    rollback = _cached_wheel_for(prior_meta)  # None on first-ever upgrade
    argv = [
        py,
        str(helper),
        str(os.getpid()),
        str(staged),
        info.latest,
        str(_config_dir()),
        str(rollback) if rollback else "",
    ]
    creationflags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW — the
        # helper must outlive us and hold no console.
        creationflags = 0x00000008 | 0x00000200 | 0x08000000
    try:
        subprocess.Popen(
            argv,
            creationflags=creationflags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return True


def _smoke_test_new_binary(install_method: str, expected_version: str, *, user: bool = False) -> tuple[bool, str]:
    """Exec `<install-path>/agnes --version` and confirm it boots AND reports
    the expected version. Resolves the binary at the install-method-specific
    path rather than via PATH — defends against a stale shadow ahead of the
    freshly-installed binary in $PATH. ``install_method`` is "uv" or "pip"
    (pip covers venv/pipx/user); ``user`` shifts the pip lookup to the per-user
    base for a ``--user`` install."""
    binary = _uv_tool_bin_path() if install_method == "uv" else _pip_bin_path(user=user)
    if binary is None:
        return False, f"agnes binary not found at expected {install_method} install path"
    try:
        env = {**os.environ, "AGNES_NO_UPDATE_CHECK": "1", _SENTINEL_ENV: "1"}
        out = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        if out.returncode != 0:
            # Windows application control (Smart App Control / WDAC) refusing
            # the freshly-installed, unsigned binary — one of the two places a
            # 4551 is observable at all, because THIS process is still running
            # (#2342; `cli/win_app_control.py` documents where it is not).
            # Return the CLASSIFIED reason rather than the raw stderr: the
            # caller keys its hint off it, and the raw text is truncated here
            # (and again by `record_outcome`), which can cut the marker out.
            if is_app_control_block(out.stderr):
                return False, block_reason(str(binary))
            return False, f"exit {out.returncode}: {out.stderr.strip()[:200]}"
        # Use Version() equality (PEP 440-aware) so "0.40.0" doesn't match "0.40.10".
        from packaging.version import InvalidVersion, Version

        tokens = out.stdout.strip().split()
        actual_str = tokens[-1] if tokens else ""
        try:
            if Version(actual_str) != Version(expected_version):
                return False, (f"version mismatch: expected {expected_version}, got {actual_str}")
        except InvalidVersion:
            return False, f"unparseable version output: {out.stdout.strip()[:80]}"
        return True, out.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        # The spawn never got off the ground. On Windows that includes an
        # application-control policy refusing the unsigned binary at LOAD
        # (`winerror == 4551`) — classified the same as the non-zero-exit
        # branch above, so the caller's hint doesn't depend on which of the
        # two shapes the block arrived in.
        if is_app_control_block(e):
            return False, block_reason(str(binary))
        return False, f"{type(e).__name__}: {e}"


def _maybe_backfill_workspace_root() -> None:
    """Record ``workspace_root`` in config for clients that pre-date the key.

    ``agnes update`` (the detached SessionStart hook) and a manual ``agnes
    self-upgrade`` both call this on every Claude Code session, so this is where
    workspaces initialized before the config-anchor existed pick it up. Writes
    ONLY when (a) config has no ``workspace_root``
    AND (b) the current directory is a genuinely-initialized workspace root —
    it carries the ``.claude/init-complete`` sentinel that ``agnes init``
    writes on success. The sentinel guard guarantees we never record a nested
    subfolder. Best-effort: any failure is swallowed so a config-write hiccup
    can't turn a SessionStart hook into a visible error.
    """
    try:
        from cli.config import get_workspace_root, set_workspace_root

        if get_workspace_root():
            return
        from cli.lib.workspace_resolve import resolve_data_workspace

        workspace = resolve_data_workspace() or Path.cwd().resolve()
        if (workspace / ".claude" / "init-complete").exists():
            set_workspace_root(str(workspace))
    except Exception:
        pass


def _try_refresh_hooks(*, quiet: bool) -> None:
    """Best-effort idempotent refresh of the workspace's Claude Code hooks.

    Resolves the workspace via
    ``cli.lib.workspace_resolve.resolve_data_workspace()`` (env override →
    shaped cwd → anchored workspace_root), falling back to the current
    working directory. Delegates the actual decision to
    :func:`maybe_refresh_claude_hooks`, which guards against writing into
    non-Agnes directories.

    Swallows any exception — a partially-broken settings.json or a
    permissions issue must not flip the exit code of a successful
    upgrade. When ``quiet`` is False the failure is surfaced to stderr
    so an operator running ``agnes self-upgrade`` interactively still sees
    it; under ``--quiet`` (the SessionStart case) it stays silent.
    """
    from cli.lib.workspace_resolve import resolve_data_workspace

    workspace = resolve_data_workspace() or Path.cwd().resolve()
    try:
        maybe_refresh_claude_hooks(workspace)
    except Exception as exc:  # pragma: no cover — defensive
        if not quiet:
            sys.stderr.write(f"agnes self-upgrade: hook refresh failed: {exc}\n")


#: Diagnostic re-probe budget. Matches `update_check`'s own probe timeout —
#: this only runs after that one already failed, so it must not add a second
#: long stall to a SessionStart hook.
_PROBE_TIMEOUT_SECONDS = 3.0


#: Short ``upgrade_status.json`` reasons per ``classify_redirect`` code. The
#: fallback covers ``unexpected_redirect`` — same address, nothing to name.
_REDIRECT_REASONS = {
    "server_moved": "server moved to {target}",
    "insecure_redirect": "server redirected to plaintext {target}",
}


def _probe_redirect(server_url: str) -> Optional[_Redirected]:
    """A redirect verdict when ``/cli/latest`` redirects, else ``None``.

    Runs only after the normal probe already came back empty, so the happy
    path pays nothing. Deliberately does not follow the redirect: httpx
    strips ``Authorization`` on a cross-origin hop, and the point here is to
    report where the server went, not to chase it. Shares the wording with
    both HTTP clients via ``cli.server_moved`` so the three cannot drift.
    """
    import httpx

    from cli.server_moved import classify_redirect, is_redirect, moved_server_message

    try:
        with httpx.Client(base_url=server_url, timeout=_PROBE_TIMEOUT_SECONDS) as c:
            resp = c.get("/cli/latest")
    except Exception:
        return None  # genuinely unreachable — the caller's existing verdict stands
    if not is_redirect(resp.status_code):
        return None
    location = resp.headers.get("Location", "")
    code, target = classify_redirect(location, server_url)
    reason = _REDIRECT_REASONS.get(code, "server redirected the update check").format(target=target)
    return _Redirected(moved_server_message(resp.status_code, location, server_url), reason)


def _resolve_info(force: bool) -> Union[UpdateInfo, _Unreachable, _Offline, _Redirected, None]:
    """Returns:
    UpdateInfo  — install this wheel
    _UNREACHABLE — --force specified, server probe failed
    _OFFLINE    — --force NOT specified, server probe failed (no opinion)
    None        — CLI is genuinely current, nothing to do
    """
    # Always invalidate the cache — an explicit `agnes self-upgrade` is
    # the user asking "is there a newer version RIGHT NOW", not "use the
    # 24h cached answer". The cache exists to keep the implicit warning
    # loop in the root callback (`agnes <anything>`) from re-probing
    # `/cli/latest` on every invocation; it has no place gating the
    # explicit upgrade command.
    _invalidate_update_cache()
    server_url = get_server_url()
    info = check(server_url, bypass_disabled=True)
    if info is None:
        # `check` is silent on every failure path, so a probe that came back
        # as a REDIRECT is indistinguishable here from a dead server. Ask
        # once, on the failure path only, before calling it unreachable.
        redirected = _probe_redirect(server_url)
        if redirected:
            return redirected
        return _UNREACHABLE if force else _OFFLINE
    if not info.download_url:
        return None
    if not force and not info.is_outdated():
        return None
    return info


def _do_install_with_smoke_and_rollback(info: UpdateInfo, *, quiet: bool) -> int:
    """Install → smoke-test → roll back on failure.

    Returns ``_INSTALL_OK`` / ``_INSTALL_FAIL`` / ``_INSTALL_DEFERRED`` (the
    caller maps DEFERRED to a no-op exit-0 that does not touch the failure
    counter).
    """
    prior_meta = _read_last_known_good_meta()  # {} on first-ever upgrade
    prior_url = prior_meta.get("download_url") or _read_last_known_good()

    # Classify how the running agnes was installed and route accordingly.
    # Refuse the two cases we must not touch: an editable checkout (the working
    # tree is the source of truth) and a system/base Python. For the rest,
    # route uv-tool → uv, everything else → pip targeting sys.executable so the
    # actually-running binary is the one upgraded.
    method, ctx = _classify_install_method()
    if method == "editable":
        sys.stderr.write(
            "agnes self-upgrade: refusing to self-upgrade an editable "
            "(pip install -e) checkout — your working tree is the source of "
            "truth. Update from git instead (git pull && uv pip install -e .).\n"
        )
        record_outcome(success=False, reason="editable install: self-upgrade refused")
        return _INSTALL_FAIL
    if method == "system":
        server = get_server_url().rstrip("/")
        sys.stderr.write(
            "agnes self-upgrade: refusing to modify a system/base Python "
            "(sys.prefix == sys.base_prefix). Reinstall into a managed "
            f"environment: curl -fsSL {server}/cli/install.sh | bash\n"
        )
        record_outcome(success=False, reason="system python: self-upgrade refused")
        return _INSTALL_FAIL

    is_user = bool(ctx.get("user"))
    smoke_method = "uv" if method == "uv-tool" else "pip"

    # WINDOWS + uv-tool: a running .exe and its loaded DLLs are locked, so
    # `uv tool install --force` fails removing the venv (os error 5) and a
    # half-done in-place swap CORRUPTS the install. Hand the swap to a detached
    # helper that runs AFTER this process exits (VS-Code-style deferred update).
    # macOS/Linux fall through to the in-place path below (POSIX can unlink a
    # running binary). If staging fails we do NOT attempt the corrupting swap.
    if sys.platform == "win32" and method == "uv-tool":
        if _spawn_windows_deferred_update(info, prior_meta, quiet=quiet):
            if not quiet:
                typer.echo(
                    "agnes self-upgrade: update staged; it finishes after this "
                    "process exits (Windows deferred install).",
                    err=True,
                )
            return _INSTALL_STAGED
        server = get_server_url().rstrip("/")
        sys.stderr.write(
            "agnes self-upgrade: could not stage the Windows deferred update; "
            "leaving the current install untouched. Recover with "
            f"curl -fsSL {server}/cli/install.sh | bash\n"
        )
        record_outcome(success=False, reason="windows: deferred-update staging failed")
        return _INSTALL_FAIL

    # PREFLIGHT (unattended only): if we recorded a prior wheel but its cached
    # artifact is missing/corrupt, an install that then fails smoke could not be
    # rolled back. On the detached/quiet SessionStart path, DEFER rather than
    # risk an unrecoverable break — retry next session. A first-ever upgrade
    # (empty prior_meta) has nothing to roll back to anyway, so it PROCEEDS;
    # blocking it would freeze every fresh install. Manual (non-quiet) runs
    # always proceed — the operator is watching and can recover via install.sh.
    if quiet and prior_meta.get("wheel_filename") and not _rollback_artifact_ok(prior_meta):
        sys.stderr.write(
            "agnes self-upgrade: deferred — no safe rollback artifact "
            "(cached prior wheel missing/corrupt); will retry next session.\n"
        )
        return _INSTALL_DEFERRED

    # Stage the wheel to the cache dir first so we only download once: install
    # from the local file, then cache the same artifact after smoke passes.
    # (Mirrors the Windows deferred-update pattern in _spawn_windows_deferred_update.)
    cache = _wheel_cache_dir()
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        cache = None  # fall back to URL-based install; second download will run

    staged: Optional[Path] = None
    if cache is not None:
        with tempfile.TemporaryDirectory(prefix="agnes_stage.") as td:
            dl = _download_wheel(info.download_url, Path(td), quiet=quiet)
            if dl is not None:
                staged_name = _staged_wheel_name(info.download_url, info.latest)
                staged_dest = cache / staged_name
                try:
                    shutil.copyfile(dl, staged_dest)
                    staged = staged_dest
                except OSError:
                    staged = None

    if staged is not None:
        rc = _install_local_wheel(staged, method=smoke_method, quiet=quiet, user=is_user)
    elif method == "uv-tool":
        rc = _install_with_uv(info.download_url, quiet=quiet)
    else:
        rc = _install_with_pip(info.download_url, quiet=quiet, user=is_user)

    if rc != 0:
        sys.stderr.write(f"agnes self-upgrade: install failed with exit {rc}\n")
        record_outcome(success=False, reason=f"install rc={rc} ({method})")
        return _INSTALL_FAIL

    ok, detail = _smoke_test_new_binary(smoke_method, expected_version=info.latest, user=is_user)
    if not ok:
        sys.stderr.write(f"agnes self-upgrade: new binary failed smoke test ({detail}).\n")
        if is_app_control_block(detail):
            # Windows refused the installed binary on policy grounds. The
            # rollback below still runs (the cached wheel may predate the
            # policy flipping to enforcing), but it regenerates an equally
            # unsigned trampoline, so it cannot be relied on — say what this
            # is and what the operator can actually do about it (#2342).
            sys.stderr.write(app_control_hint())
        server = get_server_url().rstrip("/")
        bootstrap_recovery = f"  Manual recovery: curl -fsSL {server}/cli/install.sh | bash\n"
        cached = _cached_wheel_for(prior_meta)
        if cached is not None:
            sys.stderr.write(f"  rolling back to cached {prior_meta.get('version')}\n")
            rb_rc = _install_local_wheel(cached, method=smoke_method, quiet=True, user=is_user)
            if rb_rc != 0:
                sys.stderr.write(f"  rollback ALSO failed (rc={rb_rc}); CLI is in a broken state.\n")
                sys.stderr.write(bootstrap_recovery)
        elif prior_url and prior_url != info.download_url:
            # No cached artifact (e.g. upgraded from a pre-cache version) — fall
            # back to the recorded URL. It may 404 if the server rotated its
            # wheel; that surfaces below and the recovery hint is printed.
            sys.stderr.write(f"  no cached wheel; rolling back via URL {prior_url}\n")
            rb_rc = (
                _install_with_uv(prior_url, quiet=True)
                if smoke_method == "uv"
                else _install_with_pip(prior_url, quiet=True, user=is_user)
            )
            if rb_rc != 0:
                sys.stderr.write(f"  rollback ALSO failed (rc={rb_rc}); CLI is in a broken state.\n")
                sys.stderr.write(bootstrap_recovery)
        else:
            sys.stderr.write("  no usable cached wheel for rollback; skipped.\n")
            sys.stderr.write(bootstrap_recovery)
        record_outcome(success=False, reason=f"smoke: {detail}")
        return _INSTALL_FAIL

    # Success: record the wheel cache entry. If we already staged the wheel
    # into the cache dir above, just compute its sha256 + GC. Otherwise fall
    # back to a second download (cache dir was unavailable earlier).
    cache_meta: dict = {}
    if staged is not None:
        cache_meta = _record_wheel_cache(info.latest, staged, info.download_url)
    else:
        with tempfile.TemporaryDirectory(prefix="agnes_cache.") as td:
            dl = _download_wheel(info.download_url, Path(td), quiet=True)
            if dl is not None:
                cache_meta = _record_wheel_cache(info.latest, dl, info.download_url)
    _record_last_known_good_meta({"download_url": info.download_url, "version": info.latest, **cache_meta})
    _invalidate_update_cache()
    record_outcome(success=True)  # clears any prior failure reason + resets counter
    if not quiet:
        typer.echo(f"agnes self-upgrade: installed {info.latest}", err=True)
    return _INSTALL_OK


@self_upgrade_app.callback()
def self_upgrade(
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help="Suppress progress output. Failures still surface on stderr.",
    ),
    check_only: bool = typer.Option(
        False,
        "--check-only",
        help=(
            "Print status, don't install. Exit 1 if outdated, or if the server "
            "answered the check with a redirect the CLI won't follow."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Reinstall the server's current wheel even when already on the latest version.",
    ),
) -> None:
    # Back-fill the workspace-root config anchor on every SessionStart (runs
    # before any network work so it happens even offline). No-op once set or
    # when the current dir isn't an initialized workspace root.
    _maybe_backfill_workspace_root()

    # Snapshot any prior sentinel so we restore (rather than destroy) it
    # in finally — we own the namespace but a wrapper could legitimately
    # set it.
    prior_sentinel = os.environ.get(_SENTINEL_ENV)
    os.environ[_SENTINEL_ENV] = "1"
    try:
        info = _resolve_info(force)

        # --check-only is read-only intent — never exit non-zero on
        # transport errors. If unreachable or offline, treat as "can't
        # tell, current" and exit 0 silently.
        # A redirect is the one probe failure where the server ANSWERED, so
        # it is reported before the generic ones — including under
        # --check-only, which otherwise swallows every transport verdict:
        # exiting 0 there would report "up to date" about a check that never
        # ran (--check-only's help names this second exit-1 case).
        # --quiet stays silent: that is the SessionStart hook, and its
        # non-noisy contract (#601) outranks this diagnosis.
        if isinstance(info, _Redirected):
            # `--check-only` REPORTS the redirect but changes nothing: it is
            # read-only intent, and no upgrade was attempted, so counting one
            # as failed would warn about something nobody tried. Both halves
            # of that contract already have tests
            # (`test_check_only_does_not_touch_failure_counter`,
            # `test_hook_refresh_skipped_when_check_only`) — but they only
            # exercise the outdated path, so this branch sailed past them
            # green until Devin Review on #1275 read the ordering.
            if not check_only:
                # Count it, unlike `_Offline`. That branch skips the counter
                # because a network blip is transient and would raise a false
                # alarm; a redirect is deterministic — it repeats on every
                # invocation until someone re-points the CLI. On the quiet
                # SessionStart path nothing is printed, so the #478 counter is
                # the ONLY channel that will ever mention this: without it the
                # analyst sits on a stale CLI indefinitely seeing nothing,
                # which is the exact failure this branch exists to end. At the
                # threshold the next non-quiet command says self-upgrade keeps
                # failing and names the reason, and running it prints the
                # remedy below.
                record_outcome(success=False, reason=info.reason)
                # Hook convergence is orthogonal to where /cli/latest points:
                # the workspace layout may have shifted whatever the server
                # answered, and the `_Offline` verdict this case used to reach
                # refreshed on exactly this path. Keeping it means a relocated
                # server does not also freeze the workspace's hooks until
                # someone re-points the CLI.
                _try_refresh_hooks(quiet=quiet)
            # `--force` asked for an upgrade that did not happen, so it is a
            # failure whatever `--quiet` says — `--quiet` suppresses PROGRESS,
            # not failures (its own help says so, and `_Unreachable` writes to
            # stderr and exits 1 under it). Returning 0 here told an
            # automation that its forced upgrade worked. Without `--force`
            # this is the passive check, where silence and exit 0 match
            # `_Offline`. (Devin Review on #1275.)
            if quiet and not force:
                raise typer.Exit(0)
            sys.stderr.write(f"agnes self-upgrade: {info.message}\n")
            raise typer.Exit(1)

        if check_only:
            if isinstance(info, (_Unreachable, _Offline)) or info is None or not info.is_outdated():
                raise typer.Exit(0)
            typer.echo(format_outdated_notice(info), err=True)
            raise typer.Exit(1)

        if isinstance(info, _Unreachable):
            # --force + server unreachable: an attempted upgrade that
            # couldn't even probe. Record a failure so repeated silent
            # SessionStart failures (network down for days) surface on the
            # next non-quiet command. (#478)
            record_outcome(success=False, reason="server unreachable (--force)")
            sys.stderr.write(f"agnes self-upgrade: cannot reach {get_server_url()}/cli/latest\n")
            raise typer.Exit(1)

        if isinstance(info, _Offline):
            # No --force, server unreachable: we have no opinion on
            # whether an upgrade is needed. Do NOT touch the failure
            # counter — a transient network blip (server restart, VPN
            # drop) during the SessionStart hook would otherwise reset
            # the consecutive-failure count that real install failures
            # are accumulating, hiding the warning the feature exists
            # to surface (Devin BUG_0001 on #601). Still attempt hook
            # refresh (workspace layout may have shifted) and exit 0
            # silently — quiet path is non-noisy by contract.
            _try_refresh_hooks(quiet=quiet)
            raise typer.Exit(0)

        if info is None:
            # CLI already current — server responded, version matches,
            # so the CLI is in a known-good state. Reset the failure
            # counter. Still attempt hook refresh in case the workspace
            # was initialized on an older CLI whose hook layout has
            # since changed (e.g. migrating off the removed capture-session
            # SessionStart/SessionEnd entries to the scan-based push). The
            # refresh is a no-op for directories that don't look like Agnes
            # workspaces, so an `agnes self-upgrade` invoked from ~/
            # won't write there.
            record_outcome(success=True)
            _try_refresh_hooks(quiet=quiet)
            raise typer.Exit(0)

        rc = _do_install_with_smoke_and_rollback(info, quiet=quiet)
        # NOTE: `_do_install_with_smoke_and_rollback` records the outcome itself
        # (with a specific failure reason) at each terminal branch, so we do NOT
        # re-record here — a reason-less record would clobber the detailed one.
        if rc in (_INSTALL_DEFERRED, _INSTALL_STAGED):
            # DEFERRED: unattended run with no safe rollback artifact — not
            # attempted, retry next session. STAGED: handed to the Windows
            # detached helper, which finishes after we exit and records the real
            # outcome itself. Both are benign no-ops here: exit 0, do NOT touch
            # the failure counter, still refresh hooks.
            _try_refresh_hooks(quiet=quiet)
            raise typer.Exit(0)
        if rc == _INSTALL_OK:
            # After a successful install of the new wheel, refresh the
            # workspace hooks so any wire-format change in the new release
            # lands on the next session-start without re-running
            # `agnes init`. Failure here must not turn a successful
            # upgrade into a non-zero exit — the rollback path has already
            # finished. Errors are surfaced on stderr only.
            _try_refresh_hooks(quiet=quiet)
        raise typer.Exit(rc)
    finally:
        if prior_sentinel is None:
            os.environ.pop(_SENTINEL_ENV, None)
        else:
            os.environ[_SENTINEL_ENV] = prior_sentinel
