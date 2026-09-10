"""Pure utilities used by the runner and individual processors. No DB, no
side effects beyond logging."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_jsonl(path: Path) -> list[dict]:
    """Parse a Claude Code session jsonl into a list of event dicts.

    Malformed lines are logged and skipped — a single corrupt row mustn't
    abort processing of the rest of the session. Lifted verbatim from the
    pre-refactor verification_detector.detector.parse_session so the
    behavior is identical.

    STREAMS, and must keep streaming: this is the shared path for uploaded
    CLI session transcripts (``services/session_processors/usage.py`` and
    ``verification.py``, three call sites), which run per session per
    processor tick and can be tens of MB. Iterating the file holds one
    encoded line at a time; a ``read()`` + split would hold the whole
    source AND a list of every line alongside the decoded result. It
    deliberately does NOT delegate to :func:`parse_jsonl_text` — sharing
    six lines is not worth buffering those files, and making it delegate
    was a real regression on this path (review finding on #2442).
    """
    turns: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    turns.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed JSONL line in %s", path)
    return turns


def parse_jsonl_text(text: str, *, source: object = "<text>") -> list[dict]:
    """:func:`parse_jsonl` for a caller that ALREADY holds the bytes, and
    only for such a caller — see that function on why it does not delegate
    here.

    Exists so a reader that must reason about the exact content it parsed —
    the admin transcript route, which repeats a freshness verdict made
    about one specific generation of the file — can hash and parse ONE read
    instead of opening the file twice and hoping nothing replaced it in
    between. Same parser, same skip-a-malformed-line behavior; *source*
    only names the origin in that warning.

    Splits on ``"\n"`` and NOT ``str.splitlines()``, which also breaks on
    U+2028, U+2029, \v, \f and friends. JSON permits those raw inside a
    quoted string, so a record containing one would be split into two
    invalid fragments and dropped entirely — where the file iterator
    :func:`parse_jsonl` uses breaks only on real newlines. ``strip()``
    still takes the ``\r`` off CRLF input.
    """
    turns: list[dict] = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            try:
                turns.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed JSONL line in %s", source)
    return turns


def compute_file_hash(path: Path) -> str:
    """MD5 of the file content. Used to invalidate session_processor_state
    rows when a jsonl grows (Claude Code appending to an active session)."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
