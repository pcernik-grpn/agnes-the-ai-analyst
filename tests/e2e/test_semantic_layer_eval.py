"""Does the semantic layer actually make the agent's answers better?

Every other ``real_llm`` test in this directory asks whether the plumbing works
— a frame arrived, a CLI sub-command was chosen. This one asks the only
question the semantic layer can be judged by, and it cannot be answered by a
fixture: given the same question set, does an instance WITH a semantic layer
ground its answers better than the same instance without one?

Two arms, same questions, same model, one variable:

* **baseline** — the workspace ``CLAUDE.md`` rendered with
  ``semantic_layer.has_models = False`` (no "## Semantic layer" section, so no
  "ask, don't guess" rule) and a semantic tool surface with nothing registered
  behind it.
* **semantic** — the same instance with one model registered.

Scoring is at the tool-call level, never over the prose: a table counts only
when it appears in a real call's arguments, and a metric only when its
canonical definition was actually fetched before the number was computed. An
answer that cites ``revenue/net_revenue`` in text while computing its own
``SUM(gross_amount)`` scores zero — crediting it would make the eval agree with
exactly the failure the layer exists to prevent. The fixture world, the tool
surface and the scorer live in ``tests/e2e/semantic_eval_harness.py``, and are
themselves tested — without an API key — by
``tests/test_semantic_eval_harness.py``.

Unlike the rest of this directory, nothing here needs the docker-compose stack:
the only external dependency is the Anthropic API. It still lives in
``tests/e2e/`` and carries ``real_llm`` because it belongs to the same opt-in
cost boundary (``AGNES_E2E_ANTHROPIC=1`` + ``ANTHROPIC_API_KEY``, enforced by
the collection hook in ``conftest.py``) and runs on the same CI job.

Cost/wall-clock: one run is ``2 × len(questions)`` short agent loops on a Haiku
-class model with the (large, cached) workspace prompt. Set
``AGNES_EVAL_MAX_QUESTIONS=3`` for a cheap smoke of the wiring.
"""

from __future__ import annotations

import os
import sys

import pytest

from tests.e2e.semantic_eval_harness import (
    ArmResult,
    compare,
    format_report,
    load_questions,
    run_arm,
)


pytestmark = pytest.mark.real_llm


# The gate, decided in the phase-4 plan: a baseline around 40 %, and at least
# 80 % once the layer is there. Deliberately orientational — if the first real
# runs land somewhere else, move THESE constants (they are the live record of
# what the eval demands) rather than quietly loosening the scorer.
#
# `MIN_IMPROVEMENT` is how the 40 % half is enforced without pinning a number
# no single-sample live eval can hold: what matters is that the layer moved the
# needle. It also catches the failure mode a fixed floor would miss — a
# question set so easy that both arms ace it and the eval has stopped measuring
# anything.
SEMANTIC_GATE = 0.80
BASELINE_REFERENCE = 0.40
MIN_IMPROVEMENT = 0.25

# One live sample per question, so one unlucky answer is noise rather than
# evidence. More than one control question regressing is not.
MAX_REGRESSIONS = 1


@pytest.fixture(scope="module")
def anthropic_client():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set — the eval calls a real model by design")
    try:
        import anthropic
    except ImportError:  # pragma: no cover — hard dep of the server extra
        pytest.skip("anthropic SDK not installed")
    return anthropic.Anthropic(api_key=key, timeout=120.0)


@pytest.fixture(scope="module")
def eval_run(anthropic_client) -> tuple[ArmResult, ArmResult]:
    """Run both arms once; every assertion below reads the same run.

    Module-scoped because the run IS the expensive part — three assertions
    against three separate runs would triple the spend and, worse, let the
    three disagree about which numbers they are talking about.
    """
    questions = load_questions()
    limit = int(os.environ.get("AGNES_EVAL_MAX_QUESTIONS", "0") or 0)
    if limit:
        questions = questions[:limit]

    baseline = run_arm(anthropic_client, questions, has_models=False)
    semantic = run_arm(anthropic_client, questions, has_models=True)
    # stderr, not stdout: the per-question table is the artefact an operator
    # reads when the gate moves, and it must survive pytest's capture whether
    # or not the run failed.
    print(format_report(baseline, semantic), file=sys.stderr)
    return baseline, semantic


def test_the_semantic_arm_clears_the_gate(eval_run):
    """The headline number: with a semantic layer registered, the agent grounds
    at least ``SEMANTIC_GATE`` of the question set — right table reached, right
    canonical definition read, undefined terms handed back to the user."""
    _baseline, semantic = eval_run
    assert semantic.pass_rate >= SEMANTIC_GATE, (
        f"semantic arm scored {semantic.pass_rate:.0%} (gate {SEMANTIC_GATE:.0%})\n{format_report(*eval_run)}"
    )


def test_the_layer_beats_the_baseline_by_a_real_margin(eval_run):
    """The gate above is only meaningful next to this one. A semantic arm at
    85 % against a baseline at 84 % would mean the question set, not the layer,
    is doing the work — and the 40 % baseline reference is the plan's way of
    saying the set must be hard enough to show the difference."""
    baseline, semantic = eval_run
    result = compare(baseline, semantic)
    assert result["improvement"] >= MIN_IMPROVEMENT, (
        f"baseline {baseline.pass_rate:.0%} → semantic {semantic.pass_rate:.0%} "
        f"({result['improvement']:+.0%}); the plan's reference baseline is "
        f"{BASELINE_REFERENCE:.0%} and the layer must be worth at least "
        f"{MIN_IMPROVEMENT:.0%}. Either the layer regressed or the question set "
        f"stopped discriminating.\n{format_report(*eval_run)}"
    )


def test_the_layer_does_not_break_what_already_worked(eval_run):
    """The harm case, which a rising average hides: an agent told to ask rather
    than guess can start asking about a plain row count. The control questions
    in the set exist for this, and a regression on one of them is a finding
    about the prompt, not noise."""
    baseline, semantic = eval_run
    regressions = compare(baseline, semantic)["regressions"]
    assert len(regressions) <= MAX_REGRESSIONS, (
        f"{len(regressions)} questions passed WITHOUT the semantic layer and failed with it "
        f"({', '.join(regressions)}) — the layer is making the agent worse on those.\n"
        f"{format_report(*eval_run)}"
    )
