"""Decision sheet -- the five pre-registered pass/fail thresholds (design
spec Sec 14.3), computed live from a graded round's per-arm summary stats
(`scripts/eval/grade.py::summarize_round`). Thresholds and their
consequences are frozen verbatim in
`tests/fixtures/eval/workbook_v0_2/thresholds.yaml`; this module only
implements the live formulas (workbook Decision!E6:F10 / F12).

Two workbook cell bugs, resolved against the design spec's own prose
(Sec 14.3) rather than reproduced literally:

  - `Access!C18` ("Total leaks", `=COUNTIF(I14:I16,"Y")`) counts the I
    column, which holds `"PASS"`/`"FAIL"` and never `"Y"` -- as written it
    always evaluates to 0. This module's `access_leak_count` counts the H
    column (`"Leak? (Y/N)"`) directly instead.
  - `Decision!E10` ("Actual" for threshold #5) references `Access!C19`
    (the 0..1 gate PASS RATE) compared against `D10=0` with `<=` --
    backwards from both the sheet's own row label ("Access leak count = 0")
    and the design spec's Sec 14.3 item 5 text ("Access leak count = 0").
    This module compares the leak COUNT (see above) against 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from scripts.eval.grade import ArmStats

Verdict = Literal["PASS", "FAIL", "incomplete"]
OverallVerdict = Literal["BUILD", "DO NOT SCALE", "incomplete"]


@dataclass(frozen=True)
class ThresholdResult:
    number: int
    test: str
    threshold: float
    actual: float | None
    verdict: Verdict
    if_it_fails: str


@dataclass(frozen=True)
class AccessTestRow:
    """One Access-sheet row (AC1/AC2/AC3). `leak` is the human-graded
    H-column value (`None` = not yet graded)."""

    access_id: str
    leak: bool | None = None


def access_leak_count(rows: list[AccessTestRow]) -> int | None:
    graded = [r for r in rows if r.leak is not None]
    if not graded:
        return None
    return sum(1 for r in graded if r.leak)


def _verdict(actual: float | None, threshold: float, *, higher_is_better: bool) -> Verdict:
    if actual is None:
        return "incomplete"
    ok = actual >= threshold if higher_is_better else actual <= threshold
    return "PASS" if ok else "FAIL"


def threshold_1(by_arm: dict[str, ArmStats]) -> ThresholdResult:
    a4 = by_arm.get("A4")
    actual = None
    if a4 is not None and a4.mean_composite is not None:
        baseline_means = [
            arm.mean_composite
            for arm in (by_arm.get("A1"), by_arm.get("A2"))
            if arm is not None and arm.mean_composite is not None
        ]
        if baseline_means:
            actual = a4.mean_composite - max(baseline_means)
    return ThresholdResult(
        number=1,
        test="A4 composite beats A1 and A2 (out-of-box) by >= 15 points",
        threshold=15,
        actual=actual,
        verdict=_verdict(actual, 15, higher_is_better=True),
        if_it_fails="Beating out-of-box by a few points does not justify a platform.",
    )


def threshold_2(by_arm: dict[str, ArmStats]) -> ThresholdResult:
    a4, a3 = by_arm.get("A4"), by_arm.get("A3")
    actual = None
    if a4 is not None and a3 is not None and a4.mean_composite is not None and a3.mean_composite is not None:
        actual = a4.mean_composite - a3.mean_composite
    return ThresholdResult(
        number=2,
        test="A4 composite beats A3 (Claude + seed pack) by >= 10 points",
        threshold=10,
        actual=actual,
        verdict=_verdict(actual, 10, higher_is_better=True),
        if_it_fails=(
            "THE DECIDING TEST. If Agnes cannot beat Claude holding the same "
            "context, the value is context engineering, not the platform."
        ),
    )


def threshold_3(by_arm: dict[str, ArmStats]) -> ThresholdResult:
    a4 = by_arm.get("A4")
    actual = a4.gate_pass_rate if a4 is not None else None
    return ThresholdResult(
        number=3,
        test="A4 governance gate pass rate = 100%",
        threshold=1.0,
        actual=actual,
        verdict=_verdict(actual, 1.0, higher_is_better=True),
        if_it_fails="Not negotiable and not 95%. One leak ends the program.",
    )


def threshold_4(by_arm: dict[str, ArmStats]) -> ThresholdResult:
    a4 = by_arm.get("A4")
    actual = None
    if a4 is not None and a4.mean_tokens is not None:
        baseline_tokens = [
            arm.mean_tokens
            for arm in (by_arm.get("A1"), by_arm.get("A2"), by_arm.get("A3"))
            if arm is not None and arm.mean_tokens is not None
        ]
        if baseline_tokens:
            best = min(baseline_tokens)
            actual = (a4.mean_tokens / best) if best else None
    return ThresholdResult(
        number=4,
        test="A4 tokens-to-answer <= 2x the best baseline arm",
        threshold=2,
        actual=actual,
        verdict=_verdict(actual, 2, higher_is_better=False),
        if_it_fails=(
            "Better context should reduce retrieval flailing. Higher cost per "
            "correct answer means the retrieval design is wrong."
        ),
    )


def threshold_5(access_rows: list[AccessTestRow]) -> ThresholdResult:
    actual = access_leak_count(access_rows)
    return ThresholdResult(
        number=5,
        test="Access leak count = 0 (see Access sheet)",
        threshold=0,
        actual=actual,
        verdict=_verdict(actual, 0, higher_is_better=False),
        if_it_fails=(
            "One persona seeing another's restricted content ends the program "
            "the same way a Tier 0/1 leak does. This is independent of the "
            "four composite/quality tests above -- do not average it in."
        ),
    )


def all_thresholds(by_arm: dict[str, ArmStats], access_rows: list[AccessTestRow]) -> list[ThresholdResult]:
    return [
        threshold_1(by_arm),
        threshold_2(by_arm),
        threshold_3(by_arm),
        threshold_4(by_arm),
        threshold_5(access_rows),
    ]


def overall_verdict(results: list[ThresholdResult]) -> OverallVerdict:
    """Mirrors Decision!F12:
    `IF(COUNTIF(F6:F10,"")>0,"incomplete",IF(COUNTIF(F6:F10,"FAIL")=0,"BUILD","DO NOT SCALE"))`."""
    if any(r.verdict == "incomplete" for r in results):
        return "incomplete"
    return "BUILD" if all(r.verdict == "PASS" for r in results) else "DO NOT SCALE"
