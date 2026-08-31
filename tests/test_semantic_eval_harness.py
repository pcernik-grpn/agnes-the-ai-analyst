"""The semantic-layer eval's own tests — everything except the API call.

``tests/e2e/test_semantic_layer_eval.py`` costs money and runs on one opt-in
CI job. If its scoring were only exercised there, a comparison bug would
surface as "the layer doesn't help" — a claim about the product — instead of
"the harness is broken", and nobody would be able to tell which. So the
loading, the two prompt renders, the tool surface, the scorer and the
baseline-vs-semantic comparison are all driven here over canned transcripts,
in the ordinary suite, with no key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from tests.e2e.semantic_eval_harness import (
    CATALOG,
    HARNESS_NOTE,
    METRICS,
    ArmResult,
    EvalQuestion,
    FixtureTools,
    Outcome,
    ToolCall,
    Transcript,
    asked_for_clarification,
    compare,
    computed_an_aggregate,
    format_report,
    load_questions,
    metrics_looked_up,
    render_workspace_prompt,
    run_arm,
    run_question,
    score,
    tables_reached,
)


# ---------------------------------------------------------------------------
# The question set itself
# ---------------------------------------------------------------------------


def test_the_question_set_loads_and_agrees_with_the_fixture_world():
    """Loading validates against the fixture world, so this also pins that the
    shipped YAML never names a table or metric the harness cannot serve — such
    a question would score 0 forever and read as a model failure."""
    questions = load_questions()
    assert len(questions) >= 10, "the plan asks for 10–20 questions; fewer makes the rate too coarse"
    assert len(questions) <= 20
    assert len({q.id for q in questions}) == len(questions)


def test_the_set_covers_all_four_categories():
    """A set of only declared-metric questions would show a huge improvement
    and prove nothing: without the control questions there is no evidence the
    layer leaves ordinary catalog work alone, and without the undefined-term
    ones no evidence it stops the agent inventing definitions."""
    categories = {q.category for q in load_questions()}
    assert categories == {
        "declared_metric",
        "undefined_term",
        "table_without_semantics",
        "catalog_control",
    }, f"unexpected category set: {sorted(categories)}"


def test_a_question_expecting_an_unknown_metric_is_rejected(tmp_path):
    bad = tmp_path / "q.yaml"
    bad.write_text(
        "questions:\n  - id: q1\n    question: anything?\n    expects:\n      metrics: [revenue/not_a_real_metric]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not in the fixture world"):
        load_questions(bad)


def test_a_question_cannot_demand_clarification_and_a_table_at_once(tmp_path):
    """Both halves cannot hold: the clarification branch of the scorer never
    looks at tables, so such a question silently ignores half its own spec."""
    bad = tmp_path / "q.yaml"
    bad.write_text(
        "questions:\n"
        "  - id: q1\n"
        "    question: anything?\n"
        "    expects:\n"
        "      tables: [orders]\n"
        "      must_ask_for_clarification: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="pick one"):
        load_questions(bad)


# ---------------------------------------------------------------------------
# The two arms differ by exactly the thing under test
# ---------------------------------------------------------------------------


def test_the_semantic_arm_prompt_carries_the_section_and_the_ask_dont_guess_rule():
    prompt = render_workspace_prompt(has_models=True)
    assert "## Semantic layer" in prompt
    assert "no dataset, metric, or glossary" in " ".join(prompt.split())


def test_the_baseline_arm_prompt_carries_neither():
    """The baseline is an instance with no registered model — not the same
    instance with one sentence deleted. If this ever renders the section, the
    two arms stop being a comparison of anything."""
    prompt = render_workspace_prompt(has_models=False)
    assert "## Semantic layer" not in prompt
    assert "glossary" not in prompt.lower()


def test_both_arms_still_share_the_provenance_rule():
    """The `sources` rule is not part of the experiment — it must be identical
    in both arms, or an improvement in the semantic arm could just be the
    baseline having been told less about citing its work."""
    for has_models in (True, False):
        prompt = render_workspace_prompt(has_models=has_models)
        assert "```sources" in prompt
        assert "Never report a number whose origin you cannot name." in prompt


# ---------------------------------------------------------------------------
# The fixture tool surface
# ---------------------------------------------------------------------------


def test_the_metric_tools_are_empty_without_a_layer_and_full_with_one():
    with FixtureTools(has_models=False) as tools:
        assert json.loads(tools.call("agnes_catalog_metrics", {}))["metrics"] == []
        assert "error" in json.loads(tools.call("agnes_metric_show", {"metric_id": "revenue/net_revenue"}))
    with FixtureTools(has_models=True) as tools:
        assert len(json.loads(tools.call("agnes_catalog_metrics", {}))["metrics"]) == len(METRICS)
        shown = json.loads(tools.call("agnes_metric_show", {"metric_id": "revenue/net_revenue"}))
        assert "status <> 'cancelled'" in shown["sql"]


def test_the_catalog_is_identical_in_both_arms():
    """Only the semantic half is switched — the tables, and therefore every
    control question, must look the same to both arms."""
    with FixtureTools(has_models=False) as base, FixtureTools(has_models=True) as sem:
        assert base.call("agnes_catalog", {}) == sem.call("agnes_catalog", {})
        assert len(json.loads(base.call("agnes_catalog", {}))["tables"]) == len(CATALOG)


def test_queries_run_against_real_seeded_data():
    """The tool results are real rows, not canned strings: an agent that writes
    the canonical metric SQL must be able to get an actual number back, or the
    final answer the scorer reads is meaningless."""
    with FixtureTools(has_models=True) as tools:
        out = json.loads(tools.call("agnes_query", {"sql": "SELECT COUNT(*) AS n FROM orders"}))
        assert out["rows"][0][0] == 240
        net = json.loads(tools.call("agnes_query", {"sql": METRICS["revenue/net_revenue"]["sql"]}))
        assert net["rows"][0][0] > 0


@pytest.mark.parametrize("object_type", ["glossary", "glossary_terms", "terms"])
def test_the_model_may_pluralise_or_rename_the_object_type(object_type):
    """A miss caused by the model saying "glossary_terms" instead of "glossary"
    would be scored as the agent failing to consult the layer. It isn't."""
    with FixtureTools(has_models=True) as tools:
        out = json.loads(tools.call("agnes_semantic_model_context", {"object_type": object_type}))
        assert "error" not in out, out
        assert any(entry["term"] == "active customer" for entry in out["glossary"])


def test_a_broken_query_comes_back_as_an_error_the_model_can_read():
    with FixtureTools(has_models=True) as tools:
        assert tools.call("agnes_query", {"sql": "SELECT * FROM nope"}).startswith("error:")


# ---------------------------------------------------------------------------
# Scoring — tool calls, never prose
# ---------------------------------------------------------------------------


def _q(**kw) -> EvalQuestion:
    base = {
        "id": "q",
        "category": "declared_metric",
        "question": "?",
        "tables": (),
        "metrics": (),
        "must_ask_for_clarification": False,
    }
    base.update(kw)
    return EvalQuestion(**base)  # type: ignore[arg-type]


def test_a_table_named_only_in_prose_does_not_count():
    """The failure the eval exists to catch: a confident answer *about* the
    right table, computed from somewhere else entirely."""
    question = _q(tables=("orders",))
    transcript = Transcript(
        question_id="q",
        arm="semantic",
        tool_calls=[ToolCall("agnes_query", {"sql": "SELECT COUNT(*) FROM web_sessions"})],
        final_text="Based on the orders table, there were 240 orders.",
    )
    outcome = score(question, transcript)
    assert not outcome.passed
    assert "never reached orders" in outcome.reasons[0]


def test_a_table_reached_through_a_query_counts():
    question = _q(tables=("orders", "order_refunds"))
    transcript = Transcript(
        question_id="q",
        arm="semantic",
        tool_calls=[
            ToolCall("agnes_schema", {"table": "orders"}),
            ToolCall("agnes_query", {"sql": "SELECT * FROM orders o LEFT JOIN order_refunds r USING (order_id)"}),
        ],
        final_text="12,345",
    )
    assert score(question, transcript).passed


def test_a_metric_mentioned_but_never_fetched_fails():
    question = _q(tables=("orders",), metrics=("revenue/net_revenue",))
    transcript = Transcript(
        question_id="q",
        arm="baseline",
        tool_calls=[ToolCall("agnes_query", {"sql": "SELECT SUM(gross_amount) FROM orders"})],
        final_text="Net revenue is 1,234 (revenue/net_revenue).",
    )
    outcome = score(question, transcript)
    assert not outcome.passed
    assert "canonical definition" in outcome.reasons[0]


def test_fetching_the_metric_definition_counts_either_way():
    """`agnes catalog --metrics --show <id>` and `agnes semantic-model context
    metric --id <id>` are two routes to the same definition; both are the agent
    reading the declared meaning rather than inventing one."""
    question = _q(tables=("orders",), metrics=("revenue/net_revenue",))
    for call in (
        ToolCall("agnes_metric_show", {"metric_id": "revenue/net_revenue"}),
        ToolCall("agnes_semantic_model_context", {"object_type": "metric", "id": "revenue/net_revenue"}),
        ToolCall("agnes_semantic_model_context", {"object_type": "metrics"}),
    ):
        transcript = Transcript(
            question_id="q",
            arm="semantic",
            tool_calls=[call, ToolCall("agnes_query", {"sql": "SELECT 1 FROM orders"})],
            final_text="1,234",
        )
        assert score(question, transcript).passed, call


def test_an_answer_with_no_tool_call_at_all_fails():
    transcript = Transcript(question_id="q", arm="baseline", final_text="About 1.2M.")
    outcome = score(_q(), transcript)
    assert not outcome.passed
    assert "no tool call at all" in outcome.reasons[-1]


def test_a_failed_run_is_a_failed_answer_not_a_crash():
    transcript = Transcript(question_id="q", arm="semantic", error="APIError: overloaded")
    outcome = score(_q(tables=("orders",)), transcript)
    assert not outcome.passed
    assert "run failed" in outcome.reasons[0]


# -- the undefined-term branch ----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Churn isn't defined in this instance's semantic layer.",
        "There's no canonical metric for ARR here.",
        "Could you clarify what you mean by a marketing qualified lead?",
        "How do you define customer lifetime value in your business?",
    ],
)
def test_declining_to_guess_is_recognised(text):
    assert asked_for_clarification(text)


@pytest.mark.parametrize(
    "text",
    [
        "Your churn rate last month was 4.2%.",
        "ARR is currently 1,240,000 based on the orders table.",
    ],
)
def test_a_confident_invented_answer_is_not(text):
    assert not asked_for_clarification(text)


def test_asking_and_then_answering_anyway_still_fails():
    """The hedge: ask the user what churn means, then quietly report a number
    computed from a definition of one's own. Both halves have to hold."""
    question = _q(must_ask_for_clarification=True)
    transcript = Transcript(
        question_id="q",
        arm="semantic",
        tool_calls=[ToolCall("agnes_query", {"sql": "SELECT COUNT(*) FROM customers"})],
        final_text="Churn isn't defined here, but taking lapsed customers it's about 4%.",
    )
    outcome = score(question, transcript)
    assert not outcome.passed
    assert any("computed a figure" in r for r in outcome.reasons)


def test_declining_without_computing_passes():
    question = _q(must_ask_for_clarification=True)
    transcript = Transcript(
        question_id="q",
        arm="semantic",
        tool_calls=[ToolCall("agnes_catalog_metrics", {})],
        final_text="There is no definition of churn in this semantic layer — how would you like it computed?",
    )
    assert score(question, transcript).passed


def test_exploring_the_catalog_is_not_computing():
    """Listing tables before concluding the term is undefined is diligence, not
    a guess — only an aggregate over the data is."""
    assert not computed_an_aggregate([ToolCall("agnes_query", {"sql": "SELECT * FROM orders LIMIT 5"})])
    assert computed_an_aggregate([ToolCall("agnes_query", {"sql": "select sum(gross_amount) from orders"})])


# ---------------------------------------------------------------------------
# Aggregation + comparison — the part the CI gate reads
# ---------------------------------------------------------------------------


def _arm(arm: str, passes: dict[str, bool]) -> ArmResult:
    return ArmResult(
        arm=arm,
        outcomes=[Outcome(qid, "declared_metric", arm, ok, [] if ok else ["nope"]) for qid, ok in passes.items()],
    )


def test_pass_rate_and_improvement():
    baseline = _arm("baseline", {"a": True, "b": False, "c": False, "d": False, "e": True})
    semantic = _arm("semantic", {"a": True, "b": True, "c": True, "d": True, "e": True})
    assert baseline.pass_rate == pytest.approx(0.4)
    assert semantic.pass_rate == pytest.approx(1.0)
    result = compare(baseline, semantic)
    assert result["improvement"] == pytest.approx(0.6)
    assert result["regressions"] == []


def test_a_question_the_layer_broke_is_reported_as_a_regression():
    """The gate can be green while the layer has made the agent refuse a plain
    row count. That is exactly the harm the control questions exist to find, so
    it must be named, not averaged away."""
    baseline = _arm("baseline", {"row_count": True, "net_revenue": False})
    semantic = _arm("semantic", {"row_count": False, "net_revenue": True})
    result = compare(baseline, semantic)
    assert result["improvement"] == pytest.approx(0.0)
    assert result["regressions"] == ["row_count"]


def test_an_empty_arm_scores_zero_rather_than_dividing_by_zero():
    assert ArmResult(arm="baseline", outcomes=[]).pass_rate == 0.0


def test_the_report_names_every_question_and_its_two_verdicts():
    baseline = _arm("baseline", {"row_count": True, "net_revenue": False})
    semantic = _arm("semantic", {"row_count": False, "net_revenue": True})
    report = format_report(baseline, semantic)
    assert "row_count" in report and "net_revenue" in report
    assert "REGRESSIONS" in report
    assert "50%" in report


# ---------------------------------------------------------------------------
# End-to-end over the scorer: a canned baseline vs. a canned semantic run
# ---------------------------------------------------------------------------


def test_the_whole_comparison_over_canned_transcripts():
    """The shape the real test produces, with the LLM replaced by two scripted
    runs: the baseline reaches the table but invents the calculation, the
    semantic run reads the definition first. If this ever stops separating the
    two, the live eval cannot either."""
    questions = [q for q in load_questions() if q.category == "declared_metric"][:2]

    def canned(arm: str, *, read_definition: bool) -> ArmResult:
        outcomes = []
        for q in questions:
            calls = [ToolCall("agnes_query", {"sql": f"SELECT SUM(gross_amount) FROM {' , '.join(q.tables)}"})]
            if read_definition:
                calls = [ToolCall("agnes_metric_show", {"metric_id": m}) for m in q.metrics] + calls
            outcomes.append(score(q, Transcript(q.id, arm, calls, "1,234")))
        return ArmResult(arm=arm, outcomes=outcomes)

    baseline = canned("baseline", read_definition=False)
    semantic = canned("semantic", read_definition=True)
    assert baseline.pass_rate == 0.0
    assert semantic.pass_rate == 1.0
    assert compare(baseline, semantic)["improvement"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# The agent loop, with the model replaced by a script
# ---------------------------------------------------------------------------


@dataclass
class _Text:
    text: str
    type: str = "text"


@dataclass
class _ToolUse:
    name: str
    input: dict[str, Any]
    id: str = "tu_1"
    type: str = "tool_use"


@dataclass
class _Response:
    content: list[Any]
    stop_reason: str


class _ScriptedClient:
    """Stands in for ``anthropic.Anthropic``: replays a list of responses and
    records every request, so the loop's message assembly is inspectable."""

    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs: Any) -> _Response:
        self.requests.append(kwargs)
        return self._responses.pop(0)


def test_the_loop_feeds_tool_results_back_and_records_the_calls():
    """The wiring the live test depends on: a tool_use turn must produce a real
    fixture result, be appended as a ``tool_result``, and land in the
    transcript the scorer reads."""
    client = _ScriptedClient(
        [
            _Response([_ToolUse("agnes_metric_show", {"metric_id": "revenue/net_revenue"})], "tool_use"),
            _Response([_Text("Net revenue is 1,234.")], "end_turn"),
        ]
    )
    question = EvalQuestion("q", "declared_metric", "net revenue?", ("orders",), ("revenue/net_revenue",), False)

    transcript = run_question(client, question, has_models=True)

    assert [c.name for c in transcript.tool_calls] == ["agnes_metric_show"]
    assert transcript.final_text == "Net revenue is 1,234."
    assert transcript.arm == "semantic"
    # Second request carries the assistant's tool_use turn plus our result.
    follow_up = client.requests[1]["messages"]
    assert follow_up[1]["role"] == "assistant"
    assert follow_up[2]["content"][0]["type"] == "tool_result"
    assert "status <> 'cancelled'" in follow_up[2]["content"][0]["content"]


def test_each_arm_gets_its_own_workspace_prompt_as_the_system_prompt():
    """The whole experiment is this one difference — if both arms were handed
    the same system prompt the eval would compare nothing."""
    question = EvalQuestion("q", "catalog_control", "rows?", ("orders",), (), False)
    systems: dict[bool, list[dict[str, Any]]] = {}
    for has_models in (True, False):
        client = _ScriptedClient([_Response([_Text("240")], "end_turn")])
        run_question(client, question, has_models=has_models)
        systems[has_models] = client.requests[0]["system"]
    assert "## Semantic layer" in systems[True][0]["text"]
    assert "## Semantic layer" not in systems[False][0]["text"]
    assert systems[True][0]["cache_control"] == {"type": "ephemeral"}
    # The tool-vs-shell scaffolding is not part of the experiment: identical in
    # both arms, or an improvement could just be one arm being told more.
    assert systems[True][1] == systems[False][1] == {"type": "text", "text": HARNESS_NOTE}


def test_an_api_failure_is_recorded_on_the_transcript_not_raised():
    """One overloaded call must cost one question, not the whole run — the eval
    is a rate, and a crash halfway through reports no rate at all."""

    class _Boom(_ScriptedClient):
        def create(self, **kwargs: Any) -> _Response:
            raise RuntimeError("overloaded_error")

    question = EvalQuestion("q", "catalog_control", "rows?", ("orders",), (), False)
    transcript = run_question(_Boom([]), question, has_models=True)
    assert transcript.error and "overloaded_error" in transcript.error
    assert not score(question, transcript).passed


def test_run_arm_scores_every_question_it_was_given():
    questions = load_questions()[:2]
    client = _ScriptedClient([_Response([_Text("no idea")], "end_turn") for _ in questions])
    result = run_arm(client, questions, has_models=False)
    assert [o.question_id for o in result.outcomes] == [q.id for q in questions]
    assert result.arm == "baseline"


def test_helpers_agree_with_the_scorer_on_what_was_reached():
    calls = [
        ToolCall("agnes_schema", {"table": "customers"}),
        ToolCall("agnes_query", {"sql": "SELECT country FROM CUSTOMERS GROUP BY 1"}),
        ToolCall("agnes_metric_show", {"metric_id": "customers/active_customers"}),
    ]
    assert tables_reached(calls) == {"customers"}
    assert metrics_looked_up(calls) == {"customers/active_customers"}
