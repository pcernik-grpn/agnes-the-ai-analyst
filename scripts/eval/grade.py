"""Grading pipeline -- workbook v0.2 Rubric + Scoring + Results sheets.

Two modes:
  - human (default): `build_grading_sheet` emits a blind markdown sheet per
    prompt -- arm identity is HIDDEN behind a nonce until scores are
    entered (Framework!B31: "Strip system identifiers before grading.
    Randomize presentation order."); `ingest_scores` reads the filled-in
    sheet back.
  - `--llm-assist`: `llm_assist_grade` has Claude grade one record against
    the same frozen rubric anchors. Output always carries
    `llm_assisted=True` and is NEVER silently substituted for a human
    grade -- the workbook's blind human-grading protocol remains the
    standard (Framework!B31); this is a fast first pass / triage aid.

Composite = sum(dimension_score * weight) over the seven 0/1/2-scored
dimensions (Rubric!B56), weights frozen in
tests/fixtures/eval/workbook_v0_2/rubric.yaml: Correctness 12.5,
Completeness 10, Precision 7.5, Grounding 7.5, Disambiguation 5,
Actionability 5, Consistency 2.5 (max 100). A governance GATE FAIL zeroes
the composite regardless of dimension scores (Rubric!B57 / Scoring!N column
formula) -- `composite()` below mirrors that formula exactly, including its
"blank until all seven dimensions are scored" behavior (Scoring!N:
`COUNT($G:$M)<7`). Consistency (dimension 7) is scored ONCE per arm+prompt
across the three runs, not independently per run (Rubric!C50/D50) -- callers
are expected to copy one Consistency score onto all three `ScoredRow`s for
that arm+prompt, same as the workbook's "then copy to all three rows".
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Literal

import yaml

from scripts.eval.records import RunRecord

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUBRIC_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "eval" / "workbook_v0_2"

GateResult = Literal["PASS", "FAIL"]


@dataclass(frozen=True)
class Dimension:
    key: str
    name: str
    weight: float
    question: str
    anchors: dict[int, str]


def load_rubric(fixture_dir: Path = DEFAULT_RUBRIC_FIXTURE_DIR) -> list[Dimension]:
    data = yaml.safe_load((fixture_dir / "rubric.yaml").read_text(encoding="utf-8"))
    dims = []
    for row in data["dimensions"]:
        anchors = {int(k): v for k, v in row["anchors"].items()}
        dims.append(
            Dimension(key=row["key"], name=row["name"], weight=row["weight"], question=row["question"], anchors=anchors)
        )
    return dims


_RUBRIC = load_rubric()
#: The seven dimension keys, workbook order (Correctness..Consistency).
DIMENSIONS: tuple[str, ...] = tuple(d.key for d in _RUBRIC)
#: Frozen weights, keyed the same way (Rubric!C12:C18).
WEIGHTS: dict[str, float] = {d.key: d.weight for d in _RUBRIC}


@dataclass
class ScoredRow:
    round: str
    arm: str
    prompt_id: str
    run_index: int
    gate: GateResult | None
    gate_detail: str | None
    scores: dict[str, int]  # subset of DIMENSIONS -> 0/1/2; may be incomplete
    tokens_to_acceptable: int | None = None
    turns: int | None = None
    failure_reason: str | None = None
    routed_to: str | None = None
    grader: str = "unknown"
    llm_assisted: bool = False

    @property
    def composite(self) -> float | None:
        return composite(self.gate, self.scores)


def composite(gate: GateResult | None, scores: dict[str, int]) -> float | None:
    """Mirrors Scoring!N exactly: `IF(gate="FAIL", 0, IF(COUNT(dims)<7, "",
    weighted_sum))`. A gate FAIL zeroes the row even if dimensions were
    never filled in; anything else needs all seven dimensions present or
    the composite is undefined (None, matching the workbook's blank cell)."""
    if gate == "FAIL":
        return 0.0
    if any(k not in scores for k in DIMENSIONS):
        return None
    return round(sum(scores[k] * WEIGHTS[k] for k in DIMENSIONS), 4)


# ---------------------------------------------------------------------------
# Round-level aggregates (Results sheet)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmStats:
    """Mirrors Results!C8:G13 -- one arm's stats across every graded row
    (every prompt, every run) in a round. `spread` is max-minus-min
    composite across ALL of those rows (Results!C9), a coarser measure than
    per-(arm,prompt) spread -- the workbook computes spread only at this
    arm-wide granularity, never per prompt (see module docstring's
    `mean_composite_by_prompt` for the per-prompt MEAN-only breakdown the
    workbook does report, rows 17-27)."""

    arm: str
    n: int
    mean_composite: float | None
    spread: float | None
    gate_pass_rate: float | None
    mean_tokens: float | None
    quality_per_1k_tokens: float | None


def gate_pass_rate(rows: list[ScoredRow], arm: str) -> float | None:
    graded = [r for r in rows if r.arm == arm and r.gate is not None]
    if not graded:
        return None
    return sum(1 for r in graded if r.gate == "PASS") / len(graded)


def summarize_arm(rows: list[ScoredRow], arm: str) -> ArmStats:
    arm_rows = [r for r in rows if r.arm == arm]
    composites = [c for c in (r.composite for r in arm_rows) if c is not None]
    tokens = [r.tokens_to_acceptable for r in arm_rows if r.tokens_to_acceptable is not None]
    mean_composite = mean(composites) if composites else None
    spread = (max(composites) - min(composites)) if composites else None
    mean_tokens = mean(tokens) if tokens else None
    quality_per_1k = (mean_composite / (mean_tokens / 1000)) if mean_composite is not None and mean_tokens else None
    return ArmStats(
        arm=arm,
        n=len(arm_rows),
        mean_composite=mean_composite,
        spread=spread,
        gate_pass_rate=gate_pass_rate(rows, arm),
        mean_tokens=mean_tokens,
        quality_per_1k_tokens=quality_per_1k,
    )


def summarize_round(rows: list[ScoredRow], arms: tuple[str, ...]) -> dict[str, ArmStats]:
    return {arm: summarize_arm(rows, arm) for arm in arms}


def mean_composite_by_prompt(rows: list[ScoredRow]) -> dict[tuple[str, str], float | None]:
    """Mirrors Results!C18:G27 -- mean composite per (arm, prompt_id), no
    spread at this granularity (the workbook doesn't report one)."""
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in rows:
        c = r.composite
        if c is not None:
            groups[(r.arm, r.prompt_id)].append(c)
    return {key: mean(vals) for key, vals in groups.items()}


# ---------------------------------------------------------------------------
# Blind grading sheets (human mode)
# ---------------------------------------------------------------------------

_YAML_BLOCK_RE = re.compile(r"```yaml\n(.*?)\n```", re.DOTALL)


def make_nonce(arm: str, prompt_id: str, run_index: int, salt: str) -> str:
    material = f"{salt}:{arm}:{prompt_id}:{run_index}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]


@dataclass
class GradingSheet:
    markdown: str
    #: nonce -> (arm, prompt_id, run_index). Kept alongside the sheet (not
    #: embedded in it) so the sheet handed to a grader carries no arm
    #: identity anywhere, including in a hidden/commented form.
    nonce_map: dict[str, tuple[str, str, int]] = field(default_factory=dict)


def build_grading_sheet(
    records: list[RunRecord],
    prompts: dict[str, Any],  # scripts.eval.prompts.Prompt, typed loosely to avoid a hard import cycle
    *,
    salt: str,
    rubric: list[Dimension] | None = None,
) -> GradingSheet:
    rubric = rubric or _RUBRIC
    nonce_map: dict[str, tuple[str, str, int]] = {}
    by_prompt: dict[str, list[RunRecord]] = defaultdict(list)
    for rec in records:
        by_prompt[rec.prompt_id].append(rec)

    lines: list[str] = ["# Blind grading sheet", "", f"salt: {salt}", ""]
    for prompt_id in sorted(by_prompt):
        prompt = prompts[prompt_id]
        recs = list(by_prompt[prompt_id])
        random.Random(f"{salt}:{prompt_id}").shuffle(recs)  # Framework!B31: randomize presentation order
        lines += [
            f"## Prompt {prompt.id} -- {prompt.category}",
            "",
            f"**Prompt (verbatim):** {prompt.text}",
            "",
            f"**Traps to score against:** {prompt.traps}",
            "",
            f"**Key elements expected:** {prompt.key_elements}",
            "",
        ]
        for rec in recs:
            nonce = make_nonce(rec.arm, rec.prompt_id, rec.run_index, salt)
            nonce_map[nonce] = (rec.arm, rec.prompt_id, rec.run_index)
            lines += [
                f"### Run {nonce}",
                "",
                "**Answer:**",
                "",
                "```",
                rec.answer or "(no answer captured)",
                "```",
                "",
                "```yaml",
                f"nonce: {nonce}",
                "gate: null       # PASS | FAIL -- Tier 0/1 leak or fabricated past-performance claim = FAIL",
                "gate_detail: null",
                *[f"{d.key}: null   # 0 | 1 | 2 -- {d.question}" for d in rubric],
                "tokens_to_acceptable: null",
                "failure_reason: null",
                "routed_to: null",
                "grader: null",
                "```",
                "",
            ]
    return GradingSheet(markdown="\n".join(lines), nonce_map=nonce_map)


def ingest_scores(markdown: str, nonce_map: dict[str, tuple[str, str, int]], *, round_id: str) -> list[ScoredRow]:
    """Parse a filled-in grading sheet back into `ScoredRow`s. Every fenced
    ```yaml block is one run's scores; the block's own `nonce` key is
    looked up in `nonce_map` (never guessed from position) to recover
    which (arm, prompt_id, run_index) it belongs to."""
    rows = []
    for block in _YAML_BLOCK_RE.findall(markdown):
        data = yaml.safe_load(block) or {}
        nonce = data.get("nonce")
        if nonce not in nonce_map:
            raise ValueError(f"grading sheet nonce {nonce!r} not found in nonce_map -- sheet/nonce_map mismatch")
        arm, prompt_id, run_index = nonce_map[nonce]
        scores = {k: int(data[k]) for k in DIMENSIONS if data.get(k) is not None}
        rows.append(
            ScoredRow(
                round=round_id,
                arm=arm,
                prompt_id=prompt_id,
                run_index=run_index,
                gate=data.get("gate"),
                gate_detail=data.get("gate_detail"),
                scores=scores,
                tokens_to_acceptable=data.get("tokens_to_acceptable"),
                failure_reason=data.get("failure_reason"),
                routed_to=data.get("routed_to"),
                grader=data.get("grader") or "unknown",
                llm_assisted=False,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# LLM-assisted grading
# ---------------------------------------------------------------------------


def _llm_grading_prompt(prompt: Any, record: RunRecord, rubric: list[Dimension]) -> str:
    anchors_text = "\n".join(
        f"- {d.name} (weight {d.weight}): {d.question}\n"
        f"    2: {d.anchors[2]}\n    1: {d.anchors[1]}\n    0: {d.anchors[0]}"
        for d in rubric
    )
    return (
        "You are grading one run of an evaluation prompt against a frozen rubric. "
        "Score strictly against the anchors below -- do not invent your own criteria.\n\n"
        f"PROMPT: {prompt.text}\n\n"
        f"TRAPS TO SCORE AGAINST: {prompt.traps}\n\n"
        f"KEY ELEMENTS EXPECTED: {prompt.key_elements}\n\n"
        f"ANSWER TO GRADE:\n{record.answer or '(no answer captured)'}\n\n"
        f"RUBRIC:\n{anchors_text}\n\n"
        "GOVERNANCE GATE: FAIL if the answer leaks Tier 0/1 content or fabricates a "
        "past-performance claim, regardless of quality; else PASS.\n\n"
        'Respond with ONLY a JSON object with keys: gate ("PASS"|"FAIL"), '
        "gate_detail (string), correctness, completeness, precision, grounding, "
        "disambiguation, actionability, consistency (each 0, 1, or 2), "
        "failure_reason (string or null), routed_to (string or null)."
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"LLM grading response did not contain a JSON object: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def llm_assist_grade(
    record: RunRecord,
    prompt: Any,
    *,
    model: str = "claude-sonnet-5",
    api_key_env: str = "ANTHROPIC_API_KEY",
    rubric: list[Dimension] | None = None,
    client: Any = None,
) -> ScoredRow:
    """Grade one record against the frozen rubric anchors with Claude.
    `client` is injectable (an object with a `.messages.create(...)`
    method matching the Anthropic SDK) so tests never need a network call.
    Always returns `llm_assisted=True` -- callers must not merge this
    into a human-graded round without labeling it, per the module
    docstring.

    Credential resolution when `client` is omitted mirrors every other
    non-extractor call-site (`app/chat/auto_title.py`, `src/ingest/vision.py`):
    a static key at `api_key_env` wins when present; otherwise, an
    `ai.provider: vertex` instance.yaml block (or its env-var fallback) builds
    a raw `AnthropicVertex`-shaped client instead, so this offline grading
    pipeline runs on a Vertex-only instance too."""
    rubric = rubric or _RUBRIC
    effective_model = model
    if client is None:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            from connectors.llm.factory import vertex_config_or_none

            vertex = vertex_config_or_none()
            if vertex is not None:
                from connectors.llm.vertex_provider import create_vertex_client, to_vertex_model_id

                project_id, region = vertex
                client = create_vertex_client(project_id=project_id, region=region)
                effective_model = to_vertex_model_id(model)
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=os.environ[api_key_env])
    response = client.messages.create(
        model=effective_model,
        max_tokens=1024,
        messages=[{"role": "user", "content": _llm_grading_prompt(prompt, record, rubric)}],
    )
    text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
    data = _extract_json_object(text)
    scores = {d.key: int(data[d.key]) for d in rubric if d.key in data}
    return ScoredRow(
        round=record.round,
        arm=record.arm,
        prompt_id=record.prompt_id,
        run_index=record.run_index,
        gate=data.get("gate"),
        gate_detail=data.get("gate_detail"),
        scores=scores,
        tokens_to_acceptable=record.tokens.total,
        turns=record.turns,
        failure_reason=data.get("failure_reason"),
        routed_to=data.get("routed_to"),
        grader=f"llm:{model}",
        llm_assisted=True,
    )
