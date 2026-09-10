"""Recognise and explain a Windows application-control block (WinError 4551).

Smart App Control (SAC) and WDAC refuse to **load** a binary they cannot
verify, and the refusal reads ``os error 4551`` /
``[WinError 4551] An Application Control policy has blocked this file``. Two
artifacts of a ``uv tool install`` are exactly that kind of binary: the
locally generated ``agnes.exe`` trampoline, and the portable interpreter uv
downloads. Neither is signed, so on a machine where SAC is enforcing they can
be rejected outright (#2342).

**Where a 4551 is observable — and where it is not.** The block happens at
image load. When SAC rejects ``agnes.exe`` or its interpreter as *you* launch
it, the Python process never starts: no code under ``cli/`` runs, no
``except`` in ``cli/main.py`` is reached, and a try/except around CLI startup
would be dead code that only looks like a fix. What IS catchable is a block on
a binary an **already-running** Agnes spawns — which is the self-upgrade path,
because it installs a new agnes and then execs it to verify the swap:

- ``cli/commands/self_upgrade.py::_smoke_test_new_binary`` — the freshly
  installed agnes refuses to launch (``OSError`` with ``winerror``, or a
  non-zero exit whose captured stderr carries the policy message).
- ``cli/commands/_win_deferred_update.py`` — the Windows deferred swap:
  either ``uv tool install`` itself fails, or the installed binary fails
  verification. That module must never import ``cli`` (it runs from a temp
  copy outside the tool venv it is replacing), so it carries a stdlib-only
  copy of :func:`is_app_control_block` and :func:`block_reason`;
  ``tests/test_win_app_control.py`` pins the copies to identical verdicts and
  identical wording.

There is no per-app allowlist for SAC (unlike SmartScreen's "Run anyway"), so
the only honest advice is: confirm the diagnosis, then either run agnes where
the policy does not apply or turn SAC off system-wide — knowing that lowers
protection for everything else on the machine and cannot be undone without
resetting Windows. A signed Windows build is not available yet.

Sibling hint modules, same shape (wording in one place, printed by the
caller): ``cli/query_hints.py`` ("not found" on a query surface),
``cli/server_moved.py`` (the server relocated) and ``cli/error_render.py``
(typed HTTP errors).
"""

from __future__ import annotations

#: The Win32 error an application-control policy raises (0x11C7). Windows
#: renders it as ``[WinError 4551]``; Rust-based tools such as ``uv`` as
#: ``(os error 4551)``.
APP_CONTROL_WINERROR = 4551

#: Lower-cased substrings that identify the failure in TEXT — the only shape
#: available when the block arrives as another process's captured stderr
#: (``uv`` formats Rust ``io::Error`` as ``(os error 4551)``; Python's own
#: ``str(OSError)`` as ``[WinError 4551]``). Kept deliberately narrow: a bare
#: ``"4551"`` would match any byte count.
TEXT_MARKERS: tuple[str, ...] = (
    "error 4551",
    "errno 4551",
    "application control policy has blocked",
)

#: Where the full explanation lives (symptom, how to confirm, who is exposed,
#: the workaround and its cost).
DOC_POINTER = 'docs/QUICKSTART.md → "Windows: Smart App Control blocks the CLI"'

#: The one command that turns "something is broken" into a diagnosis.
CONFIRM_COMMAND = r'reg query "HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy" /v VerifiedAndReputablePolicyState'


def _basename(path: str) -> str:
    """Last path segment, for either separator — a Windows path is being
    formatted by code that usually runs on Windows but is tested on Linux, so
    ``os.path.basename`` would return the whole string here."""
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or path


def is_app_control_block(failure: object) -> bool:
    """True iff ``failure`` is a Windows application-control block.

    Accepts either shape the CLI can hold: an exception from a spawn (keyed on
    the ``winerror`` attribute, which only Windows sets — so no
    ``sys.platform`` branch is needed and the predicate is testable
    everywhere) or a text blob (another process's captured stderr, or a reason
    string this module produced and persisted earlier).
    """
    if failure is None:
        return False
    if getattr(failure, "winerror", None) == APP_CONTROL_WINERROR:
        return True
    text = str(failure).lower()
    return any(marker in text for marker in TEXT_MARKERS)


def block_reason(binary: str | None = None) -> str:
    """One short line naming the block, for a persisted failure reason.

    Sized for ``upgrade_status.json``'s 200-char ``last_failure_reason`` (see
    ``cli/upgrade_status.py``) because that field is what a later, non-quiet
    command echoes. It stays recognisable to :func:`is_app_control_block` on
    the way back out — ``should_warn()`` re-classifies the recorded reason
    rather than keeping a second vocabulary.
    """
    what = _basename(binary) if binary else "the agnes binary"
    return (
        f"windows application control blocked {what} (os error 4551) — "
        f"Smart App Control refuses unsigned binaries; see {DOC_POINTER}"
    )


def hint(binary: str | None = None) -> str:
    """The actionable multi-line message to write to stderr, newline-terminated.

    States what happened, how to confirm it, what the only workaround costs,
    and where the full explanation lives — it never claims a signed build
    exists, because none does yet.
    """
    what = binary or "the agnes binary"
    return (
        "\n"
        "Windows application control blocked this file:\n"
        f"  {what}\n"
        "Smart App Control refuses binaries it cannot verify, and both the\n"
        "agnes.exe uv generates and the interpreter uv downloads are unsigned.\n"
        "The refusal happens at load, before any agnes code runs — so this is\n"
        "not an agnes crash, and reinstalling produces the same unsigned files.\n"
        "  Confirm:  " + CONFIRM_COMMAND + "\n"
        "            2 = enforcing, 1 = evaluation, 0 = off.\n"
        "  Next:     there is no per-app allowlist for Smart App Control. Run\n"
        "            agnes under WSL or a Linux container, or turn Smart App\n"
        "            Control off in Windows Security → App & browser control.\n"
        "            That switch is system-wide, lowers protection for the whole\n"
        "            machine, and cannot be re-enabled without resetting Windows.\n"
        "            A signed Windows build is not available yet.\n"
        f"  Details:  {DOC_POINTER}\n"
    )
