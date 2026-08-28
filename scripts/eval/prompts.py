"""Loads the frozen ten-prompt set (workbook v0.2, Prompts sheet).

The prompt strings are FROZEN as of 2026-08-27 (the workbook's README sheet
STATUS line: "Do not edit prompt text, weights, or thresholds below without
versioning as v0.3 and re-grading R0 under the new version"). This module
refuses to load a locally-edited copy of the fixture: `frozen_manifest.json`
pins a sha256 of `prompts.yaml` as committed, and `load_prompts` recomputes
it at read time, raising `FrozenPromptsTamperedError` on any mismatch --
including whitespace-only edits.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "eval" / "workbook_v0_2"

#: The ten frozen prompt ids, in workbook Prompts-sheet row order.
PROMPT_IDS: tuple[str, ...] = ("X1", "P1", "P2", "T1", "T2", "A1", "L1", "N1", "G1", "G2")

#: The five evaluation arms (Framework sheet Sec 14.1).
ARMS: tuple[str, ...] = ("A0", "A1", "A2", "A3", "A4")

#: Arms this harness can drive directly against a live API (A0: Anthropic;
#: A4: Agnes). A1/A2/A3 run in external product UIs and are manual-transcript
#: import only -- see scripts/eval/arms.py module docstring.
API_DRIVEN_ARMS: tuple[str, ...] = ("A0", "A4")
MANUAL_ARMS: tuple[str, ...] = ("A1", "A2", "A3")

RUNS_PER_PROMPT = 3


class FrozenPromptsTamperedError(RuntimeError):
    """`prompts.yaml`'s sha256 no longer matches `frozen_manifest.json` --
    the fixture was edited locally. The workbook is frozen (STATUS line);
    a real change must be versioned as v0.3 and R0 re-graded under it, not
    silently applied here."""


@dataclass(frozen=True)
class Prompt:
    id: str
    category: str
    service_line: str
    text: str
    tests: str
    why_baselines_struggle: str
    traps: str
    key_elements: str
    source_ref: str
    gradeable: bool
    built_by: str
    date_built: str


def verify_frozen(fixture_dir: Path = DEFAULT_FIXTURE_DIR) -> None:
    """Raise `FrozenPromptsTamperedError` if `prompts.yaml` no longer
    matches the sha256 pinned in `frozen_manifest.json`. Called by
    `load_prompts`; exposed separately so callers that only need the
    integrity check (no parsing) don't pay for the YAML parse."""
    prompts_path = fixture_dir / "prompts.yaml"
    manifest_path = fixture_dir / "frozen_manifest.json"
    raw = prompts_path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest["prompts_sha256"]
    if actual != expected:
        raise FrozenPromptsTamperedError(
            f"{prompts_path} sha256 mismatch: got {actual}, frozen manifest "
            f"pins {expected}. The workbook v0.2 prompt set is FROZEN "
            f"(2026-08-27) -- do not edit prompt text; version to v0.3 and "
            f"re-grade R0 under the new version if a change is truly needed."
        )


def load_prompts(fixture_dir: Path = DEFAULT_FIXTURE_DIR) -> list[Prompt]:
    """Load and validate the frozen prompt set. Raises
    `FrozenPromptsTamperedError` on a sha256 mismatch before parsing
    anything -- an edited prompt string never silently reaches a run."""
    verify_frozen(fixture_dir)
    prompts_path = fixture_dir / "prompts.yaml"
    data = yaml.safe_load(prompts_path.read_text(encoding="utf-8"))
    rows = data["prompts"]
    prompts = [
        Prompt(
            id=row["id"],
            category=row["category"],
            service_line=row["service_line"],
            text=row["text"],
            tests=row["tests"],
            why_baselines_struggle=row["why_baselines_struggle"],
            traps=row["traps"],
            key_elements=row["key_elements"],
            source_ref=row["source_ref"],
            gradeable=bool(row["gradeable"]),
            built_by=row["built_by"],
            date_built=row["date_built"],
        )
        for row in rows
    ]
    found_ids = tuple(p.id for p in prompts)
    if found_ids != PROMPT_IDS:
        raise ValueError(
            f"prompts.yaml prompt ids {found_ids} do not match the expected "
            f"frozen set {PROMPT_IDS} -- fixture is corrupt or was hand-edited "
            f"in a way the sha256 check didn't catch (unexpected)."
        )
    return prompts


def load_prompts_by_id(fixture_dir: Path = DEFAULT_FIXTURE_DIR) -> dict[str, Prompt]:
    return {p.id: p for p in load_prompts(fixture_dir)}
