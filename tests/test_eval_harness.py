"""EQ0 self-test (design spec Sec 15.3): the eval harness matches workbook
v0.2 exactly -- ten prompts, five arms, three runs, 0/1/2 anchors, frozen
weights, governance gate, spread reporting, frozen-prompt tamper detection.

Plus unit tests for composite math (hand-verified against the Rubric
sheet's own worked example), blind-nonce hiding, decision-threshold
verdicts, and the EQ3/EQ9 metrics math on a tiny synthetic manifest.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import fields
from pathlib import Path

import httpx
import jsonschema
import pytest

from scripts.eval import decision, grade, metrics
from scripts.eval.arms import AgnesArm, AnthropicArm, ArmExecutionError, ChatSurface
from scripts.eval.config import RunConfig, RunConfigError
from scripts.eval.grade import ArmStats, ScoredRow
from scripts.eval.prompts import (
    ARMS,
    DEFAULT_FIXTURE_DIR,
    PROMPT_IDS,
    RUNS_PER_PROMPT,
    FrozenPromptsTamperedError,
    load_prompts,
    load_prompts_by_id,
    verify_frozen,
)
from scripts.eval.records import RunRecord, TokenCounts, Turn, iter_records, read_record, write_record

# ---------------------------------------------------------------------------
# EQ0 -- harness matches workbook v0.2 exactly
# ---------------------------------------------------------------------------


def test_ten_frozen_prompt_ids_in_workbook_order():
    prompts = load_prompts()
    assert tuple(p.id for p in prompts) == PROMPT_IDS
    assert PROMPT_IDS == ("X1", "P1", "P2", "T1", "T2", "A1", "L1", "N1", "G1", "G2")
    assert len(prompts) == 10


def test_every_prompt_is_gradeable_and_has_traps_and_key_elements():
    for p in load_prompts():
        assert p.gradeable is True
        assert p.text.strip()
        assert p.traps.strip()
        assert p.key_elements.strip()


def test_five_arms():
    assert ARMS == ("A0", "A1", "A2", "A3", "A4")


def test_three_runs_per_prompt():
    assert RUNS_PER_PROMPT == 3


def test_rubric_anchors_are_0_1_2_only():
    rubric = grade.load_rubric()
    assert len(rubric) == 7
    for dim in rubric:
        assert set(dim.anchors) == {0, 1, 2}


def test_exact_frozen_weights():
    assert grade.WEIGHTS == {
        "correctness": 12.5,
        "completeness": 10,
        "precision": 7.5,
        "grounding": 7.5,
        "disambiguation": 5,
        "actionability": 5,
        "consistency": 2.5,
    }
    # Rubric!B9: weights were rescaled x2.5 from the old 0-5 scale so the
    # composite still tops out at 100 -- since the new max score per
    # dimension is 2 (not 1), the weights themselves sum to 50, and the
    # MAXIMUM contribution (weight x 2) is what sums to 100.
    assert sum(grade.WEIGHTS.values()) == 50
    assert sum(w * 2 for w in grade.WEIGHTS.values()) == 100


def test_gate_fail_zeroes_the_question_even_with_full_scores():
    full_scores = {k: 2 for k in grade.DIMENSIONS}
    assert grade.composite("FAIL", full_scores) == 0.0
    assert grade.composite("FAIL", {}) == 0.0  # gate fail zeroes even ungraded dimensions


def test_composite_is_none_when_dimensions_incomplete():
    partial = {k: 2 for k in list(grade.DIMENSIONS)[:6]}  # one short of all seven
    assert grade.composite("PASS", partial) is None
    assert grade.composite(None, partial) is None


def test_spread_present_in_the_report_schema():
    field_names = {f.name for f in fields(ArmStats)}
    assert "spread" in field_names
    assert "mean_composite" in field_names
    assert "gate_pass_rate" in field_names


def test_frozen_prompt_sha_check_refuses_tampering(tmp_path):
    fixture_copy = tmp_path / "workbook_v0_2"
    shutil.copytree(DEFAULT_FIXTURE_DIR, fixture_copy)

    verify_frozen(fixture_copy)  # untouched copy still verifies clean

    prompts_path = fixture_copy / "prompts.yaml"
    prompts_path.write_text(prompts_path.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")

    with pytest.raises(FrozenPromptsTamperedError):
        verify_frozen(fixture_copy)
    with pytest.raises(FrozenPromptsTamperedError):
        load_prompts(fixture_copy)


def test_frozen_manifest_pins_a_2026_08_27_date():
    manifest = json.loads((DEFAULT_FIXTURE_DIR / "frozen_manifest.json").read_text(encoding="utf-8"))
    assert manifest["frozen"] == "2026-08-27"


# ---------------------------------------------------------------------------
# Composite math -- hand-verified against Rubric!B62:B65's worked example
# ---------------------------------------------------------------------------


def test_composite_matches_rubric_worked_example():
    # Rubric sheet: Correct=1 Complete=2 Precision=2 Ground=1 Disambig=1
    # Action=2 Consist=2 -> composite 75.0 (Rubric!B65 "Check:" line).
    scores = {
        "correctness": 1,
        "completeness": 2,
        "precision": 2,
        "grounding": 1,
        "disambiguation": 1,
        "actionability": 2,
        "consistency": 2,
    }
    assert grade.composite("PASS", scores) == 75.0


def test_scored_row_composite_property():
    scores = {k: 2 for k in grade.DIMENSIONS}
    row = ScoredRow(round="R1", arm="A4", prompt_id="X1", run_index=1, gate="PASS", gate_detail=None, scores=scores)
    assert row.composite == 100.0


def test_summarize_arm_mean_spread_and_quality_per_1k_tokens():
    full = {k: 2 for k in grade.DIMENSIONS}  # composite 100
    half = {k: 1 for k in grade.DIMENSIONS}  # composite 50
    rows = [
        ScoredRow(
            round="R1",
            arm="A4",
            prompt_id="X1",
            run_index=1,
            gate="PASS",
            gate_detail=None,
            scores=full,
            tokens_to_acceptable=1000,
        ),
        ScoredRow(
            round="R1",
            arm="A4",
            prompt_id="X1",
            run_index=2,
            gate="PASS",
            gate_detail=None,
            scores=half,
            tokens_to_acceptable=2000,
        ),
        ScoredRow(
            round="R1",
            arm="A4",
            prompt_id="P1",
            run_index=1,
            gate="FAIL",
            gate_detail="leak",
            scores={},
            tokens_to_acceptable=500,
        ),
    ]
    stats = grade.summarize_arm(rows, "A4")
    assert stats.n == 3
    assert stats.mean_composite == pytest.approx((100 + 50 + 0) / 3)
    assert stats.spread == pytest.approx(100 - 0)
    assert stats.gate_pass_rate == pytest.approx(2 / 3)
    assert stats.mean_tokens == pytest.approx((1000 + 2000 + 500) / 3)
    assert stats.quality_per_1k_tokens == pytest.approx(stats.mean_composite / (stats.mean_tokens / 1000))


def test_mean_composite_by_prompt():
    full = {k: 2 for k in grade.DIMENSIONS}
    rows = [
        ScoredRow(round="R1", arm="A4", prompt_id="X1", run_index=1, gate="PASS", gate_detail=None, scores=full),
        ScoredRow(round="R1", arm="A4", prompt_id="X1", run_index=2, gate="PASS", gate_detail=None, scores=full),
    ]
    by_prompt = grade.mean_composite_by_prompt(rows)
    assert by_prompt[("A4", "X1")] == 100.0


# ---------------------------------------------------------------------------
# Blind grading -- nonce hides arm identity, ingest recovers it exactly
# ---------------------------------------------------------------------------


def test_blind_nonce_hides_arm_identity_in_the_sheet():
    prompts = load_prompts_by_id()
    records = [
        RunRecord(
            round="R1",
            arm="A0",
            prompt_id="X1",
            run_index=1,
            persona=None,
            source="api",
            transcript=[],
            started_at="2026-08-27T00:00:00Z",
            completed_at="2026-08-27T00:00:01Z",
            tokens=TokenCounts(),
            turns=1,
            answer="ANSWER_FROM_ARM_ONE",
        ),
        RunRecord(
            round="R1",
            arm="A4",
            prompt_id="X1",
            run_index=1,
            persona="principal",
            source="api",
            transcript=[],
            started_at="2026-08-27T00:00:00Z",
            completed_at="2026-08-27T00:00:01Z",
            tokens=TokenCounts(),
            turns=1,
            answer="ANSWER_FROM_ARM_TWO",
        ),
    ]
    sheet = grade.build_grading_sheet(records, prompts, salt="test-salt")
    assert "A0" not in sheet.markdown
    assert "A4" not in sheet.markdown
    assert "principal" not in sheet.markdown
    assert "ANSWER_FROM_ARM_ONE" in sheet.markdown
    assert "ANSWER_FROM_ARM_TWO" in sheet.markdown
    assert len(sheet.nonce_map) == 2
    recovered_arms = {arm for arm, _, _ in sheet.nonce_map.values()}
    assert recovered_arms == {"A0", "A4"}


def test_ingest_scores_round_trip_via_nonce_map():
    nonce_map = {"abc12345": ("A4", "X1", 1)}
    filled = """# sheet
### Run abc12345
```yaml
nonce: abc12345
gate: PASS
gate_detail: null
correctness: 2
completeness: 2
precision: 2
grounding: 2
disambiguation: 2
actionability: 2
consistency: 2
tokens_to_acceptable: 1500
failure_reason: null
routed_to: null
grader: alice
```
"""
    rows = grade.ingest_scores(filled, nonce_map, round_id="R1")
    assert len(rows) == 1
    row = rows[0]
    assert (row.arm, row.prompt_id, row.run_index) == ("A4", "X1", 1)
    assert row.gate == "PASS"
    assert row.grader == "alice"
    assert row.tokens_to_acceptable == 1500
    assert row.composite == 100.0


def test_ingest_scores_rejects_unknown_nonce():
    filled = """```yaml
nonce: doesnotexist
gate: PASS
```
"""
    with pytest.raises(ValueError):
        grade.ingest_scores(filled, {}, round_id="R1")


def test_llm_assist_grade_is_labeled_and_never_silent():
    prompts = load_prompts_by_id()
    prompt = prompts["X1"]
    record = RunRecord(
        round="R1",
        arm="A0",
        prompt_id="X1",
        run_index=1,
        persona=None,
        source="api",
        transcript=[],
        started_at="2026-08-27T00:00:00Z",
        completed_at=None,
        tokens=TokenCounts(input_tokens=100, output_tokens=50),
        turns=1,
        answer="Some answer.",
    )

    class _FakeBlock:
        type = "text"
        text = json.dumps(
            {
                "gate": "PASS",
                "gate_detail": None,
                "correctness": 1,
                "completeness": 2,
                "precision": 2,
                "grounding": 1,
                "disambiguation": 1,
                "actionability": 2,
                "consistency": 2,
                "failure_reason": None,
                "routed_to": None,
            }
        )

    class _FakeResponse:
        content = [_FakeBlock()]

    class _FakeMessages:
        def create(self, **kwargs):
            return _FakeResponse()

    class _FakeClient:
        messages = _FakeMessages()

    row = grade.llm_assist_grade(record, prompt, client=_FakeClient())
    assert row.llm_assisted is True
    assert row.grader.startswith("llm:")
    assert row.composite == 75.0  # same worked-example scores as the Rubric sheet


# ---------------------------------------------------------------------------
# Decision thresholds -- verdicts (design spec Sec 14.3)
# ---------------------------------------------------------------------------


def _arm_stats(arm: str, *, mean_composite=None, gate_pass_rate=None, mean_tokens=None) -> ArmStats:
    return ArmStats(
        arm=arm,
        n=30,
        mean_composite=mean_composite,
        spread=0.0,
        gate_pass_rate=gate_pass_rate,
        mean_tokens=mean_tokens,
        quality_per_1k_tokens=None,
    )


def test_threshold_verdicts_all_pass_build():
    by_arm = {
        "A0": _arm_stats("A0", mean_composite=30),
        "A1": _arm_stats("A1", mean_composite=40, mean_tokens=2000),
        "A2": _arm_stats("A2", mean_composite=35, mean_tokens=1800),
        "A3": _arm_stats("A3", mean_composite=60, mean_tokens=2500),
        "A4": _arm_stats("A4", mean_composite=75, gate_pass_rate=1.0, mean_tokens=3000),
    }
    access_rows = [
        decision.AccessTestRow("AC1", leak=False),
        decision.AccessTestRow("AC2", leak=False),
        decision.AccessTestRow("AC3", leak=False),
    ]
    results = decision.all_thresholds(by_arm, access_rows)
    assert [r.verdict for r in results] == ["PASS", "PASS", "PASS", "PASS", "PASS"]
    assert decision.overall_verdict(results) == "BUILD"


def test_threshold_2_is_the_deciding_test_and_fails_the_program():
    by_arm = {
        "A1": _arm_stats("A1", mean_composite=40, mean_tokens=2000),
        "A2": _arm_stats("A2", mean_composite=35, mean_tokens=1800),
        "A3": _arm_stats("A3", mean_composite=70, mean_tokens=2500),  # close to A4
        "A4": _arm_stats("A4", mean_composite=75, gate_pass_rate=1.0, mean_tokens=3000),
    }
    access_rows = [decision.AccessTestRow("AC1", leak=False)]
    t2 = decision.threshold_2(by_arm)
    assert t2.actual == pytest.approx(5.0)
    assert t2.verdict == "FAIL"
    results = decision.all_thresholds(by_arm, access_rows)
    assert decision.overall_verdict(results) == "DO NOT SCALE"


def test_threshold_3_requires_100_percent_not_95():
    by_arm = {"A4": _arm_stats("A4", gate_pass_rate=0.95)}
    t3 = decision.threshold_3(by_arm)
    assert t3.verdict == "FAIL"


def test_threshold_5_uses_leak_count_not_pass_rate():
    """Regression pin for the two Decision-sheet cell bugs this module
    deliberately does NOT reproduce (see decision.py module docstring)."""
    rows = [
        decision.AccessTestRow("AC1", leak=True),
        decision.AccessTestRow("AC2", leak=False),
        decision.AccessTestRow("AC3", leak=False),
    ]
    assert decision.access_leak_count(rows) == 1
    t5 = decision.threshold_5(rows)
    assert t5.actual == 1
    assert t5.verdict == "FAIL"

    clean_rows = [decision.AccessTestRow("AC1", leak=False), decision.AccessTestRow("AC2", leak=False)]
    assert decision.threshold_5(clean_rows).verdict == "PASS"


def test_overall_verdict_incomplete_when_a_threshold_has_no_actual_yet():
    by_arm = {"A4": _arm_stats("A4")}
    results = decision.all_thresholds(by_arm, [])
    assert decision.overall_verdict(results) == "incomplete"


# ---------------------------------------------------------------------------
# EQ3/EQ9 metrics math -- tiny synthetic ground-truth manifest
#
# The canonical ground-truth shape is the corpus generator's (see
# scripts/eval/corpus_gen.py's module docstring and
# tests/fixtures/eval/planted_corpus_small/ground_truth.json): `nodes[]`/
# `edges[]` producer wire rows, one row per evidence-bearing claim, no
# top-level `facts`/`document_count`/`corpus_id`. The pure math functions
# below (`precision_recall_by_type`, `cluster_purity`) are unchanged --
# they still take a plain `{natural_key, type, aliases, ...}` list, which is
# what `metrics.expected_facts_from_manifest` derives from `nodes[]`.
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SMALL_FIXTURE_PATH = _REPO_ROOT / "tests" / "fixtures" / "eval" / "planted_corpus_small" / "ground_truth.json"

# Directly-constructed input for the pure math functions -- independent of
# the manifest/loader adapters, same shape and values as before reconciling
# the two ground-truth tasks.
_SYNTHETIC_FACTS = [
    {"natural_key": "myers", "type": "client", "aliases": ["myers-eps"], "source_doc_ids": ["document:d1"]},
    {"natural_key": "acme", "type": "client", "source_doc_ids": ["document:d2"]},
]

_SYNTHETIC_ACTUAL_SUBJECTS = [
    {"id": "f1", "type": "client", "aliases": ["myers"], "attrs": {}, "claim_count": 1, "quote_count": 1},
    {
        "id": "f2",
        "type": "client",
        "aliases": ["other-key"],
        "attrs": {"industry": {"conflicted": True, "values": ["a", "b"]}},
        "claim_count": 1,
        "quote_count": 1,
    },
]


def _synthetic_document(doc_id: str, name: str) -> dict:
    return {
        "doc_id": doc_id,
        "stable_id": f"local:{name}",
        "name": name,
        "path": f"site-a/lib-a/{name}",
        "site": "site-a",
        "drive": "lib-a",
        "source": "local",
        "mime": "text/markdown",
        "size": 100,
        "created": "2026-01-01T00:00:00+00:00",
        "modified": "2026-01-01T00:00:00+00:00",
        "author": "a@example.com",
        "last_editor": "a@example.com",
        "sha256": "0" * 64,
        "extracted_path": None,
        "extract_status": "ok",
        "crawled_at": None,
    }


# Canonical-shaped manifest -- the same two planted facts as _SYNTHETIC_FACTS,
# but expressed as producer wire rows: "myers" is revisited by a second claim
# from a different document (dedupe-by-id), and one edge is planted twice
# from two documents (dedupe-by src+type+dst).
_SYNTHETIC_GROUND_TRUTH = {
    "generator": {
        "name": "test-generator",
        "version": "0.0.0",
        "seed": 1,
        "mode": "small",
        "generated_at": "2026-08-27T00:00:00+00:00",
    },
    "groups": ["principal"],
    "sites": {"site-a": {"libraries": {"lib-a": {"groups": ["principal"]}}}},
    "documents": [
        _synthetic_document("document:d1", "d1.md"),
        _synthetic_document("document:d2", "d2.md"),
    ],
    "nodes": [
        {
            "claim_key": "n1",
            "id": "client:myers",
            "type": "client",
            "attrs": {},
            "evidence": [{"doc_id": "document:d1", "quote": "Myers Corp is a client."}],
        },
        {
            "claim_key": "n1b",
            "id": "client:myers",
            "type": "client",
            "attrs": {"industry": "manufacturing"},
            "evidence": [{"doc_id": "document:d2", "quote": "Myers Corp operates in manufacturing."}],
        },
        {
            "claim_key": "n2",
            "id": "client:acme",
            "type": "client",
            "attrs": {},
            "evidence": [{"doc_id": "document:d2", "quote": "Acme Inc is a client."}],
        },
    ],
    "edges": [
        {
            "claim_key": "e1",
            "src": "client:myers",
            "type": "owned_by",
            "dst": "org:acme-holding",
            "attrs": {},
            "evidence": [{"doc_id": "document:d1", "quote": "Myers is owned by Acme Holding."}],
        },
        {
            "claim_key": "e1b",
            "src": "client:myers",
            "type": "owned_by",
            "dst": "org:acme-holding",
            "attrs": {},
            "evidence": [{"doc_id": "document:d2", "quote": "Myers Corp, owned by Acme Holding."}],
        },
    ],
    "prepared_false_claims": [],
    "planted_elements": {"s_fixtures": {}, "traps": {}, "anonymization": {}},
    "counts": {"documents": 2, "planted_documents": 2, "filler_documents": 0, "sites": 1, "nodes": 3, "edges": 2},
}


def test_ground_truth_schema_is_valid_json_schema():
    schema = json.loads(metrics.GROUND_TRUTH_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(instance=_SYNTHETIC_GROUND_TRUTH, schema=schema)


def test_ground_truth_schema_rejects_missing_required_field():
    schema = json.loads(metrics.GROUND_TRUTH_SCHEMA_PATH.read_text(encoding="utf-8"))
    bad = {k: v for k, v in _SYNTHETIC_GROUND_TRUTH.items() if k != "nodes"}  # missing required "nodes"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad, schema=schema)


def test_load_ground_truth_validates_a_file(tmp_path):
    path = tmp_path / "gt.json"
    path.write_text(json.dumps(_SYNTHETIC_GROUND_TRUTH), encoding="utf-8")
    loaded = metrics.load_ground_truth(path)
    assert loaded["counts"]["documents"] == 2


def test_real_planted_corpus_small_validates_against_schema():
    """The corpus generator's own committed fixture is the reference
    instance of this schema (per the schema's own `description`) -- a
    drift between the two means one of the two tasks changed shape without
    telling the other."""
    ground_truth = metrics.load_ground_truth(_SMALL_FIXTURE_PATH)  # raises jsonschema.ValidationError on drift
    assert ground_truth["counts"]["nodes"] > 0
    assert ground_truth["counts"]["edges"] > 0


def test_expected_facts_from_manifest_dedupes_by_id():
    facts = metrics.expected_facts_from_manifest(_SYNTHETIC_GROUND_TRUTH)
    by_key = {f["natural_key"]: f for f in facts}
    assert set(by_key) == {"client:myers", "client:acme"}
    # the two "myers" claims (from d1 and d2) collapse into one expected fact
    assert sorted(by_key["client:myers"]["source_doc_ids"]) == ["document:d1", "document:d2"]
    assert by_key["client:myers"]["type"] == "client"


def test_expected_edges_from_manifest_dedupes_by_src_type_dst():
    edges = metrics.expected_edges_from_manifest(_SYNTHETIC_GROUND_TRUTH)
    assert len(edges) == 1  # e1/e1b share (src, type, dst)
    edge = edges[0]
    assert (edge["src_natural_key"], edge["type"], edge["dst_natural_key"]) == (
        "client:myers",
        "owned_by",
        "org:acme-holding",
    )
    assert sorted(edge["source_doc_ids"]) == ["document:d1", "document:d2"]


def test_expected_facts_and_edges_dedupe_the_real_fixture():
    ground_truth = metrics.load_ground_truth(_SMALL_FIXTURE_PATH)
    facts = metrics.expected_facts_from_manifest(ground_truth)
    edges = metrics.expected_edges_from_manifest(ground_truth)
    # 19 planted node claims collapse onto 14 distinct ids (some facts are
    # revisited by a second claim/document, e.g. the AN2 Czech-inflection
    # pair); every planted edge already has a distinct (src, type, dst).
    assert len(facts) == 14
    assert len(edges) == 28


def test_precision_recall_by_type_on_synthetic_manifest():
    results = metrics.precision_recall_by_type(_SYNTHETIC_FACTS, _SYNTHETIC_ACTUAL_SUBJECTS)
    client = results["client"]
    assert client.true_positives == 1  # f1 matched myers via alias overlap
    assert client.false_positives == 1  # f2 matched nothing planted
    assert client.false_negatives == 1  # acme never matched
    assert client.precision == pytest.approx(0.5)
    assert client.recall == pytest.approx(0.5)


def test_precision_recall_counts_a_duplicate_subject_as_a_false_positive():
    """Two extracted subjects matching the SAME planted cluster used to both
    increment `true_positives` while the cluster was recorded matched once,
    so TP could exceed the planted count and precision was overstated —
    which matters because EQ3 feeds a pre-registered decision threshold. A
    second subject for one planted fact is an entity-resolution failure: the
    first is the true positive, the extra one is a false positive (Devin
    Review on #1652)."""
    planted = [{"type": "client", "natural_key": "client:myers", "aliases": ["client:myers-eps"]}]
    actual = [
        {"type": "client", "id": "s1", "aliases": ["client:myers"]},
        {"type": "client", "id": "s2", "aliases": ["client:myers-eps"]},  # same planted fact
    ]
    r = metrics.precision_recall_by_type(planted, actual)["client"]
    assert r.true_positives == 1, "one planted fact can be matched at most once"
    assert r.false_positives == 1, "the duplicate subject is a spurious extra, not a second hit"
    assert r.false_negatives == 0
    assert r.true_positives <= len(planted)
    assert r.precision == pytest.approx(0.5)
    assert r.recall == pytest.approx(1.0), "recall must stay within [0, 1]"


def test_precision_recall_prefers_an_unmatched_cluster_over_a_taken_one():
    """Greedy first-hit assignment must not manufacture a false positive: a
    subject overlapping both a taken cluster and a free one is credited to
    the free one, so input order cannot change the score."""
    planted = [
        {"type": "client", "natural_key": "client:a", "aliases": ["shared:x"]},
        {"type": "client", "natural_key": "client:b", "aliases": ["shared:x"]},
    ]
    actual = [
        {"type": "client", "id": "s1", "aliases": ["client:a", "shared:x"]},
        {"type": "client", "id": "s2", "aliases": ["shared:x", "client:b"]},
    ]
    r = metrics.precision_recall_by_type(planted, actual)["client"]
    assert (r.true_positives, r.false_positives, r.false_negatives) == (2, 0, 0)


def test_cluster_purity_on_synthetic_manifest():
    purity = metrics.cluster_purity(_SYNTHETIC_FACTS, _SYNTHETIC_ACTUAL_SUBJECTS)
    # planted alias-instance total = |{myers,myers-eps}| + |{acme}| = 3
    # best overlap: myers cluster vs f1 = 1, acme cluster vs anything = 0
    assert purity == pytest.approx(1 / 3)


def test_conflict_rate_per_1000_docs():
    rate = metrics.conflict_rate_per_1000_docs(_SYNTHETIC_ACTUAL_SUBJECTS, document_count=500)
    assert rate == pytest.approx(1 / 500 * 1000)


def test_orphan_rate_treats_missing_subject_as_orphan():
    edge_counts = {"f1": 2, "f2": 0}
    rate = metrics.orphan_rate(["f1", "f2", "f3"], edge_counts)
    assert rate == pytest.approx(2 / 3)  # f2 (0 edges) and f3 (missing) are orphans


def test_orphan_rate_empty_is_none():
    assert metrics.orphan_rate([], {}) is None


def test_probe_facts_endpoint_returns_false_on_unreachable_server():
    assert metrics.probe_facts_endpoint("http://127.0.0.1:1", "", timeout=0.5) is False


# The facts REST API lands in a parallel build-order step (design spec Sec
# 16 step 6) -- skip the live integration test until it (or an operator-set
# AGNES_EVAL_BASE_URL pointing at a build that has it) actually exists.
_EVAL_BASE_URL = os.environ.get("AGNES_EVAL_BASE_URL", "")
_FACTS_ENDPOINT_AVAILABLE = bool(_EVAL_BASE_URL) and metrics.probe_facts_endpoint(
    _EVAL_BASE_URL, os.environ.get("AGNES_EVAL_TOKEN", "")
)


@pytest.mark.skipif(
    not _FACTS_ENDPOINT_AVAILABLE,
    reason="facts REST API not available (AGNES_EVAL_BASE_URL unset or /api/facts/search absent)",
)
def test_fetch_and_score_release_against_a_live_server():
    record = metrics.fetch_and_score_release(
        _EVAL_BASE_URL,
        os.environ.get("AGNES_EVAL_TOKEN", ""),
        _SYNTHETIC_GROUND_TRUTH,
        release_id="test",
        corpus_id="test-corpus",
    )
    assert "cluster_purity" in record


# ---------------------------------------------------------------------------
# Records -- round trip
# ---------------------------------------------------------------------------


def test_run_record_round_trip(tmp_path):
    record = RunRecord(
        round="R1",
        arm="A0",
        prompt_id="X1",
        run_index=1,
        persona=None,
        source="api",
        transcript=[Turn(role="user", content="hi", ts="2026-08-27T00:00:00Z")],
        started_at="2026-08-27T00:00:00Z",
        completed_at="2026-08-27T00:00:01Z",
        tokens=TokenCounts(input_tokens=10, output_tokens=5),
        turns=1,
        answer="hi back",
    )
    path = write_record(tmp_path, record)
    assert path == tmp_path / "R1" / "A0" / "X1_1.json"
    loaded = read_record(path)
    assert loaded == record

    all_records = list(iter_records(tmp_path, "R1"))
    assert all_records == [record]
    scoped_records = list(iter_records(tmp_path, "R1", arm="A0"))
    assert scoped_records == [record]
    assert list(iter_records(tmp_path, "R1", arm="A4")) == []
    assert list(iter_records(tmp_path, "does-not-exist")) == []


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_run_config_requires_round_and_arms():
    with pytest.raises(RunConfigError):
        RunConfig.from_dict({"arms": ["A0"]})
    with pytest.raises(RunConfigError):
        RunConfig.from_dict({"round": "R1", "arms": []})


def test_run_config_defaults():
    config = RunConfig.from_dict({"round": "R1", "arms": ["A0", "A4"]})
    assert config.runs_per_prompt == RUNS_PER_PROMPT
    assert config.output_dir == Path("runs")
    assert config.prompt_ids is None


# ---------------------------------------------------------------------------
# Arms -- fast failure paths (no network)
# ---------------------------------------------------------------------------


def test_anthropic_arm_records_missing_api_key_without_network_call(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    arm = AnthropicArm(api_key_env="ANTHROPIC_API_KEY")
    prompt = load_prompts_by_id()["X1"]
    record = arm.run(prompt, persona=None, run_index=1, round_id="R1")
    assert record.errors
    assert "ANTHROPIC_API_KEY" in record.errors[0]
    assert record.tokens.total is None


def test_agnes_arm_records_missing_persona_env_without_network_call(monkeypatch):
    monkeypatch.delenv("AGNES_EVAL_TOKEN_PRINCIPAL", raising=False)
    surface = ChatSurface("https://agnes.example.com", "eval-agent")
    arm = AgnesArm(surface, persona_tokens={"principal": "AGNES_EVAL_TOKEN_PRINCIPAL"})
    prompt = load_prompts_by_id()["X1"]
    record = arm.run(prompt, persona="principal", run_index=1, round_id="R1")
    assert record.errors
    assert "AGNES_EVAL_TOKEN_PRINCIPAL" in record.errors[0]


def test_chat_surface_200_path_via_mock_transport(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"answer": "the answer"})

    surface = ChatSurface("https://agnes.example.com", "eval-agent", transport=httpx.MockTransport(handler))
    result = surface.ask("hello", token="test-token", timeout_s=5)
    assert result["answer"] == "the answer"


def test_chat_surface_202_poll_path_via_mock_transport():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            return httpx.Response(202, json={"job_id": "job-1"})
        calls["n"] += 1
        return httpx.Response(200, json={"status": "completed", "result": {"answer": "polled answer"}})

    surface = ChatSurface("https://agnes.example.com", "eval-agent", transport=httpx.MockTransport(handler))
    result = surface.ask("hello", token="test-token", timeout_s=5)
    assert result["answer"] == "polled answer"
    assert calls["n"] == 1


def test_chat_surface_raises_on_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    surface = ChatSurface("https://agnes.example.com", "eval-agent", transport=httpx.MockTransport(handler))
    with pytest.raises(ArmExecutionError):
        surface.ask("hello", token="test-token", timeout_s=5)
