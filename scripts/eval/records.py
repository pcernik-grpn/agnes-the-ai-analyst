"""The machine-readable run record -- "the run is the artifact" (design spec
Sec 15.5). Every arm executor and every manual-transcript import produces one
`RunRecord`, written as one JSON file per (round, arm, prompt, run#) under
`runs/<round>/<arm>/<prompt>_<run#>.json`. Nothing is ever appended in place:
a re-run of the same triple overwrites its own file, so the run directory
always reflects the latest attempt and a crash mid-round loses at most the
one in-flight run.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal

RunSource = Literal["api", "manual"]


@dataclass(frozen=True)
class Turn:
    """One turn of a transcript. `role` is a free-text label ("user",
    "assistant", "tool", "system") -- the harness does not interpret it,
    only preserves it for the grading sheet and for a human re-reading the
    transcript later."""

    role: str
    content: str
    ts: str | None = None


@dataclass(frozen=True)
class TokenCounts:
    """Whatever token counts are actually known for this run. Every field
    is optional because visibility differs by arm (README!C24 / Framework!
    B45:D48, see `tests/fixtures/eval/workbook_v0_2/token_methods.md`) --
    an absent count must never render as a misleading 0."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None

    @property
    def total(self) -> int | None:
        """input + output only (the two counts every arm's method reports).
        Cache tokens are tracked separately and flagged, never folded in --
        the workbook explicitly warns against letting a cache hit "look
        artificially cheap" (README!C24, arm A3)."""
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit(self) -> bool:
        return bool(self.cache_read_tokens)


@dataclass
class RunRecord:
    round: str
    arm: str
    prompt_id: str
    run_index: int  # 1..RUNS_PER_PROMPT
    persona: str | None
    source: RunSource
    transcript: list[Turn]
    started_at: str  # ISO 8601 UTC
    completed_at: str | None
    tokens: TokenCounts
    turns: int
    errors: list[str] = field(default_factory=list)
    answer: str | None = None
    notes: str | None = None
    raw: dict[str, Any] | None = None  # opaque provider payload, for audit

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunRecord":
        d = dict(data)
        d["transcript"] = [Turn(**t) for t in d.get("transcript", [])]
        d["tokens"] = TokenCounts(**d.get("tokens", {}))
        return cls(**d)


def record_path(runs_dir: Path, round_id: str, arm: str, prompt_id: str, run_index: int) -> Path:
    return runs_dir / round_id / arm / f"{prompt_id}_{run_index}.json"


def write_record(runs_dir: Path, record: RunRecord) -> Path:
    path = record_path(runs_dir, record.round, record.arm, record.prompt_id, record.run_index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_record(path: Path) -> RunRecord:
    return RunRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))


def iter_records(runs_dir: Path, round_id: str, arm: str | None = None) -> Iterator[RunRecord]:
    """Yield every record for `round_id`, optionally scoped to one `arm`.
    Silently skips a round/arm directory that doesn't exist yet (an
    unrun arm is not an error at this layer)."""
    round_dir = runs_dir / round_id
    if not round_dir.is_dir():
        return
    arm_dirs = [round_dir / arm] if arm else sorted(p for p in round_dir.iterdir() if p.is_dir())
    for arm_dir in arm_dirs:
        if not arm_dir.is_dir():
            continue
        for path in sorted(arm_dir.glob("*.json")):
            yield read_record(path)
