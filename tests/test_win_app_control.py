"""Windows application control (Smart App Control / WDAC) blocks unsigned
binaries with WinError 4551 — #2342.

Every test here runs on Linux. The Windows failure is synthesized in the two
shapes Agnes can actually observe from an already-running process:

1. a Python ``OSError`` carrying ``winerror = 4551`` (a spawn that never got
   off the ground), and
2. a captured stderr blob from ``uv`` or the ``agnes.exe`` trampoline holding
   ``os error 4551`` / the policy message.

What is deliberately NOT tested, because it is not reachable in-process: a
block on ``agnes.exe`` (or its interpreter) at *launch*. The image is refused
at load, so the Python process never starts and no handler inside ``cli/``
ever runs — see ``cli/win_app_control.py``'s module docstring.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli import win_app_control as wac
from cli.commands import _win_deferred_update as h


def _win_oserror(path: str = r"C:\Users\a\.local\bin\agnes.exe") -> OSError:
    """The exact shape ``subprocess`` raises on Windows when the image is
    refused at load: ``errno`` is the mapped POSIX code, ``winerror`` carries
    the real 4551. Constructible on any OS, which is why the CLI-side
    predicate keys on the attribute rather than on ``sys.platform``."""
    exc = OSError(13, "An Application Control policy has blocked this file")
    exc.winerror = 4551  # type: ignore[attr-defined]
    exc.filename = path
    return exc


# Real-world text shapes: uv (Rust `io::Error`) and Python's own str(OSError).
_UV_STDERR = (
    "error: Failed to install agnes-the-ai-analyst\n"
    "  Caused by: failed to create hardlink from python.exe: "
    "An Application Control policy has blocked this file. (os error 4551)"
)
_PY_STDERR = (
    r"[WinError 4551] An Application Control policy has blocked this file: "
    r"'C:\Users\a\.local\bin\agnes.exe'"
)
_UNRELATED = (
    "failed to remove directory ...Scripts: Access is denied. (os error 5)",
    'The wheel filename "0.72.4.whl" is invalid: Must have a version',
    "ImportError: cannot import name 'foo'",
    "",
    "scanned 4551 bytes",
)


# --------------------------------------------------------------------------- #
# The predicate
# --------------------------------------------------------------------------- #


def test_detects_oserror_with_winerror():
    assert wac.is_app_control_block(_win_oserror()) is True


@pytest.mark.parametrize("text", [_UV_STDERR, _PY_STDERR])
def test_detects_captured_stderr(text):
    assert wac.is_app_control_block(text) is True


@pytest.mark.parametrize("text", _UNRELATED)
def test_ignores_unrelated_failures(text):
    assert wac.is_app_control_block(text) is False


def test_ignores_unrelated_oserror():
    assert wac.is_app_control_block(OSError(5, "Access is denied")) is False
    assert wac.is_app_control_block(None) is False


# --------------------------------------------------------------------------- #
# The wording
# --------------------------------------------------------------------------- #


def test_hint_is_actionable():
    hint = wac.hint(binary=r"C:\Users\a\.local\bin\agnes.exe")
    # names the blocked file
    assert "agnes.exe" in hint
    # how to confirm the diagnosis
    assert "VerifiedAndReputablePolicyState" in hint
    # the honest cost of the only workaround
    assert "no per-app allowlist" in hint.lower()
    assert "system-wide" in hint.lower()
    # points forward, per command-ux.md ("not found must point forward")
    assert "docs/QUICKSTART.md" in hint
    assert hint.endswith("\n")


def test_hint_does_not_promise_a_signed_build():
    """A signed Windows binary needs signing infrastructure that does not
    exist yet (#2342 ask 3). The hint must not imply it is available."""
    hint = wac.hint().lower()
    assert "not available" in hint or "not yet" in hint


def test_reason_fits_the_persisted_status_field():
    from cli import upgrade_status as us

    reason = wac.block_reason(binary=r"C:\Users\a\.local\bin\agnes.exe")
    assert len(reason) <= us._MAX_REASON_LEN
    assert us._redact(reason) == reason  # survives the secret scrubber intact
    assert "agnes.exe" in reason
    assert "4551" in reason


def test_persisted_reason_is_itself_recognizable():
    """`should_warn` re-classifies the recorded reason rather than keeping a
    second vocabulary, so the reason string must match the predicate."""
    assert wac.is_app_control_block(wac.block_reason()) is True


# --------------------------------------------------------------------------- #
# Deferred-update helper: it may not import `cli`, so it carries a stdlib-only
# copy of the predicate. Pin the two to identical verdicts.
# --------------------------------------------------------------------------- #


def test_deferred_helper_predicate_matches_shared_module():
    assert h._APP_CONTROL_MARKERS == wac.TEXT_MARKERS
    assert h._APP_CONTROL_WINERROR == wac.APP_CONTROL_WINERROR
    samples = (_UV_STDERR, _PY_STDERR, *_UNRELATED, _win_oserror(), OSError(5, "Access is denied"), None)
    for sample in samples:
        assert h._looks_like_app_control(sample) is wac.is_app_control_block(sample), sample


def test_deferred_helper_reason_wording_matches_shared_module():
    assert h._app_control_reason("agnes.exe") == wac.block_reason("agnes.exe")


def test_app_control_is_not_mistaken_for_a_file_lock():
    """`_uv_install` retries a Windows file lock for up to ~5 minutes. An
    application-control block never clears on its own, so it must not be
    classified as a lock."""
    assert h._looks_like_lock(_UV_STDERR) is False


# --------------------------------------------------------------------------- #
# Reachable site 1 — self_upgrade's smoke test (the freshly installed agnes
# is spawned by an agnes that IS running).
# --------------------------------------------------------------------------- #


def test_smoke_test_translates_spawn_oserror():
    """The detail must be the CLASSIFIED reason, not the raw exception text:
    the caller keys its hint off it, and the raw text is truncated on the way
    into `upgrade_status.json` (which could cut the marker out)."""
    from cli.commands import self_upgrade as su

    with (
        patch.object(su, "_uv_tool_bin_path", return_value=Path("/fake/bin/agnes.exe")),
        patch.object(su.subprocess, "run", side_effect=_win_oserror("/fake/bin/agnes.exe")),
    ):
        ok, detail = su._smoke_test_new_binary("uv", expected_version="0.40.0")
    assert ok is False
    assert detail == wac.block_reason("/fake/bin/agnes.exe")


def test_smoke_test_translates_trampoline_stderr():
    """The trampoline itself launches, but the interpreter it loads is
    refused — the block arrives as stderr text plus a non-zero exit. A long
    uv error must still classify, hence the padding."""
    from cli.commands import self_upgrade as su

    long_stderr = ("uv is retrying the hardlink strategy; " * 12) + _UV_STDERR
    with (
        patch.object(su, "_uv_tool_bin_path", return_value=Path("/fake/bin/agnes.exe")),
        patch.object(su.subprocess, "run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr=long_stderr)
        ok, detail = su._smoke_test_new_binary("uv", expected_version="0.40.0")
    assert ok is False
    assert detail == wac.block_reason("/fake/bin/agnes.exe")


def test_self_upgrade_prints_hint_and_records_reason(tmp_path, monkeypatch):
    """End to end: a smoke failure classified as an application-control block
    prints the actionable hint and persists a reason naming it."""
    from typer.testing import CliRunner

    from cli import upgrade_status as us
    from cli.main import app
    from cli.update_check import UpdateInfo

    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AGNES_SELF_UPGRADE_IN_PROGRESS", raising=False)
    monkeypatch.setattr("cli.commands.self_upgrade._classify_install_method", lambda: ("uv-tool", {}))

    blocked = (False, wac.block_reason(binary=r"C:\Users\a\.local\bin\agnes.exe"))
    with (
        patch(
            "cli.commands.self_upgrade.check",
            return_value=UpdateInfo(
                installed="0.30.0",
                latest="0.40.0",
                download_url="http://server.test/cli/wheel/agnes-0.40.0-py3-none-any.whl",
            ),
        ),
        patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"),
        patch("cli.commands.self_upgrade.subprocess.run", return_value=MagicMock(returncode=0)),
        patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=blocked),
        patch("cli.commands.self_upgrade._read_last_known_good", return_value=None),
        patch("cli.commands.self_upgrade.get_server_url", return_value="http://server.test"),
        patch("cli.commands.self_upgrade.maybe_refresh_claude_hooks"),
    ):
        result = CliRunner().invoke(app, ["self-upgrade"])

    assert result.exit_code == 1
    assert "VerifiedAndReputablePolicyState" in result.stderr
    assert "docs/QUICKSTART.md" in result.stderr
    assert wac.is_app_control_block(us.read_status().get("last_failure_reason", ""))


# --------------------------------------------------------------------------- #
# Reachable site 2 — the Windows deferred swap (install + verify both run in
# the helper, with stderr captured).
# --------------------------------------------------------------------------- #


class _FakeProc:
    def __init__(self, returncode, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def test_deferred_install_block_fails_fast_with_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(h, "_venv_python", lambda: None)
    monkeypatch.setattr(h, "_venv_free", lambda py: True)
    monkeypatch.setattr(h.time, "sleep", lambda s: pytest.fail("must not retry a policy block"))
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return _FakeProc(2, stderr=_UV_STDERR)

    monkeypatch.setattr(h.subprocess, "run", fake_run)

    rc, detail = h._uv_install("x.whl", config_dir=str(tmp_path), budget_s=5.0, backoff_s=0.01)
    assert rc == 2
    assert len(calls) == 1
    assert h._looks_like_app_control(detail), detail


def test_deferred_verify_block_surfaces_reason(monkeypatch):
    monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(_win_oserror()))
    ok, detail = h._installed_version_ok("0.72.9")
    assert ok is False
    assert h._looks_like_app_control(detail), detail


def test_deferred_run_records_app_control_reason(monkeypatch, tmp_path):
    """The helper runs detached with no console, so `upgrade_status.json` is
    its only channel to the human — the reason it writes must name the block."""
    import json

    monkeypatch.setattr(h, "_wait_for_exit", lambda pid, **k: None)
    monkeypatch.setattr(
        h,
        "_uv_install",
        lambda wheel, **k: (2, h._app_control_reason("python.exe")),
    )
    cfg = tmp_path / "cfg"
    cfg.mkdir()

    assert h.run(1, str(tmp_path / "x.whl"), "0.72.3", str(cfg), None) == 2
    status = json.loads((cfg / "upgrade_status.json").read_text())
    assert wac.is_app_control_block(status["last_failure_reason"]), status


# --------------------------------------------------------------------------- #
# Surfacing — a block never self-heals, so it must not wait three silent
# failures to reach the human.
# --------------------------------------------------------------------------- #


def test_app_control_failure_warns_on_the_first_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli import upgrade_status as us

    us.record_outcome(False, reason=wac.block_reason(binary="agnes.exe"))
    assert us.consecutive_failures() == 1
    assert us.should_warn() is True
    notice = us.format_failure_notice()
    assert "failed 1 time " in notice or notice.count("failed 1 time") == 1
    assert "1 times" not in notice
    assert "4551" in notice

    us.mark_warned()
    assert us.should_warn() is False  # still once per failure level


def test_ordinary_failure_still_waits_for_the_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli import upgrade_status as us

    us.record_outcome(False, reason="smoke: version mismatch")
    assert us.should_warn() is False
