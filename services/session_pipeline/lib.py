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
    behavior is identical."""
    with open(path) as f:
        return parse_jsonl_text(f.read(), source=path)


def parse_jsonl_text(text: str, *, source: object = "<text>") -> list[dict]:
    """:func:`parse_jsonl` for a caller that ALREADY holds the bytes.

    Same parser, same skip-a-malformed-line behavior; *source* only names
    the origin in that warning. Exists so a reader that must reason about
    the exact content it parsed — the admin transcript route, which repeats
    a freshness verdict that was made about one specific generation of the
    file — can hash and parse ONE read instead of opening the file twice
    and hoping nothing replaced it in between."""
    turns: list[dict] = []
    for line in text.splitlines():
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
