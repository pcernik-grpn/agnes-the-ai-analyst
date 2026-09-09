"""Persist + surface the outcome of self-upgrade attempts (#478).

The SessionStart hook runs a detached `agnes update --quiet` (see
`cli/lib/hooks.py`), whose CLI step invokes the self-upgrade installer
(`_do_install_with_smoke_and_rollback`). The child is fully detached with
stdout/stderr suppressed, so any failure (network, uv/pip resolution,
smoke-test rollback) is invisible. An analyst can sit on a stale CLI for
weeks with no signal.

This module records each self-upgrade outcome to
``$AGNES_CONFIG_DIR/upgrade_status.json``::

    {"last_attempt_ts": 1718000000.0,
     "last_outcome": "failure",
     "consecutive_failures": 3}

The quiet SessionStart path stays silent but increments the counter on
failure and resets it on success. The next NON-quiet `agnes` command emits
a one-line stderr warning (once, via the root-callback banner path in
``cli/main.py``) when ``consecutive_failures >= _WARN_THRESHOLD`` — or on the
FIRST failure when the recorded reason is a Windows application-control block,
which never clears on its own (#2342). ``--quiet`` commands never warn.

Best-effort throughout: a read/write failure here must never break a
working `agnes` command — that is the whole point of the feature.
"""

from __future__ import annotations

import json
import re
import time
from typing import Optional

from cli.config import _config_dir
from cli.win_app_control import is_app_control_block

_STATUS_FILENAME = "upgrade_status.json"

# Number of consecutive silent self-upgrade failures before the next
# non-quiet command surfaces a warning. Owner default = 3.
_WARN_THRESHOLD = 3

# Cap + secret-scrub for the persisted failure reason. upgrade_status.json is
# NOT 0600 (unlike token.json), and the smoke-test detail can embed
# `stderr[:200]` which might echo a signed wheel URL carrying a token — so
# redact before persisting.
_MAX_REASON_LEN = 200
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(AGNES_TOKEN|authorization|bearer)\b\s*[:=]?\s*\S+"),
    re.compile(r"(?i)[?&](token|access_token|sig|signature|api[_-]?key|key)=[^\s&]+"),
    re.compile(r"\b[A-Fa-f0-9]{64,}\b"),  # long hex runs (tokens / hashes); 64+ avoids git SHAs
]


def _redact(reason: str) -> str:
    """Scrub obvious secrets from a failure reason and cap its length."""
    s = reason
    for pat in _SECRET_PATTERNS:
        s = pat.sub("[REDACTED]", s)
    return s[:_MAX_REASON_LEN]


def _status_path():
    return _config_dir() / _STATUS_FILENAME


def read_status() -> dict:
    """Return the persisted status dict, or ``{}`` on missing/malformed file."""
    p = _status_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_status(entry: dict) -> None:
    p = _status_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(entry), encoding="utf-8")
    except OSError:
        pass  # best-effort — a status-write failure must not break the flow


def record_outcome(success: bool, *, reason: Optional[str] = None, now: Optional[float] = None) -> None:
    """Record a self-upgrade attempt outcome.

    On success the consecutive-failure counter resets to 0 (and any prior
    failure reason is cleared); on failure it increments from the prior value
    and — when ``reason`` is given — persists a redacted one-line
    ``last_failure_reason`` so the next non-quiet command can surface WHY it
    failed, not just that it did. ``last_outcome`` is ``"success"`` or
    ``"failure"`` and ``last_attempt_ts`` is the wall-clock time of the attempt.
    """
    ts = time.time() if now is None else now
    prior = read_status()
    prior_failures = prior.get("consecutive_failures", 0)
    if not isinstance(prior_failures, int) or prior_failures < 0:
        prior_failures = 0
    if success:
        entry = {
            "last_attempt_ts": ts,
            "last_outcome": "success",
            "consecutive_failures": 0,
        }
    else:
        entry = {
            "last_attempt_ts": ts,
            "last_outcome": "failure",
            "consecutive_failures": prior_failures + 1,
        }
        if reason:
            entry["last_failure_reason"] = _redact(reason)
    _write_status(entry)


def consecutive_failures() -> int:
    """How many self-upgrade attempts have failed in a row (0 if unknown)."""
    n = read_status().get("consecutive_failures", 0)
    return n if isinstance(n, int) and n >= 0 else 0


def should_warn() -> bool:
    """True iff self-upgrade has failed enough times to be worth surfacing
    AND we have not already warned at this exact failure count.

    "Enough times" is ``_WARN_THRESHOLD`` for an ordinary failure — a network
    blip or a transient lock is usually gone by the next session, and warning
    on the first one would be noise. One class of failure is exempt: a Windows
    application-control block (#2342) is a policy decision, not a transient,
    so it never clears on its own and the analyst is stuck on a stale CLI
    until they act. Waiting three sessions to say so would be three sessions
    of silence about something already permanent.

    The "already warned at this count" check keeps the warning to ONCE per
    distinct failure level: a non-quiet command at 3 failures warns; the
    next non-quiet command (still at 3) stays silent. A fresh failure
    (4, 5, …) re-arms the warning so the analyst sees that the situation
    is getting worse, not better."""
    s = read_status()
    n = s.get("consecutive_failures", 0)
    if not isinstance(n, int) or n < 1:
        return False
    if n < _WARN_THRESHOLD and not is_app_control_block(s.get("last_failure_reason")):
        return False
    warned_at = s.get("warned_at_failures")
    return warned_at != n


def mark_warned() -> None:
    """Record that we surfaced the warning at the current failure count, so
    subsequent non-quiet commands at the same level stay silent."""
    s = read_status()
    n = s.get("consecutive_failures", 0)
    if not (isinstance(n, int) and n >= 0):
        return
    s["warned_at_failures"] = n
    _write_status(s)


def format_failure_notice() -> str:
    """One-line stderr warning for repeated silent self-upgrade failures.

    Appends the recorded (already-redacted) ``last_failure_reason`` when present
    so the analyst sees WHY it's failing without having to re-run the command.
    """
    n = consecutive_failures()
    times = "time" if n == 1 else "times"
    base = f"agnes self-upgrade has failed {n} {times} — run `agnes self-upgrade` to see the error."
    reason = read_status().get("last_failure_reason")
    if isinstance(reason, str) and reason:
        base += f" Last error: {reason}"
    return base
