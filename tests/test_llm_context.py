"""The LLM call context — a contextvar every producer reads so a span and a
ledger row can say who ran the call, for what, in which turn."""

from __future__ import annotations

import asyncio

from src.observability.llm_context import (
    WORKLOADS,
    LlmCallContext,
    bind_llm_context,
    current_llm_context,
    llm_context,
    unbind_llm_context,
)


def test_the_default_context_is_empty():
    ctx = current_llm_context()
    assert ctx == LlmCallContext()
    assert ctx.span_attributes() == {}


def test_nested_contexts_merge_and_inner_wins():
    with llm_context(workload="builder", purpose="entity_builder_turn", user_id="u1"):
        with llm_context(purpose="inner", subject_id="ent_1"):
            ctx = current_llm_context()
            assert ctx.workload == "builder"  # inherited
            assert ctx.purpose == "inner"  # inner wins
            assert ctx.user_id == "u1"
            assert ctx.subject_id == "ent_1"
        assert current_llm_context().purpose == "entity_builder_turn"  # restored
    assert current_llm_context() == LlmCallContext()


def test_a_none_override_never_clears_an_inherited_value():
    with llm_context(workload="chat", user_id="u1"):
        with llm_context(user_id=None):
            assert current_llm_context().user_id == "u1"


def test_span_attributes_carry_only_set_fields():
    ctx = LlmCallContext(workload="ocr", purpose="scan_ocr", job_id="job_1")
    assert ctx.span_attributes() == {
        "agnes.workload": "ocr",
        "agnes.purpose": "scan_ocr",
        "agnes.job_id": "job_1",
    }


def test_bind_and_unbind_token():
    token = bind_llm_context(job_id="job_9")
    assert current_llm_context().job_id == "job_9"
    unbind_llm_context(token)
    assert current_llm_context().job_id is None


def test_context_is_task_local():
    async def _child():
        return current_llm_context().workload

    async def _main():
        with llm_context(workload="extraction"):
            return await asyncio.create_task(_child())

    assert asyncio.run(_main()) == "extraction"
    assert current_llm_context().workload is None


def test_workload_vocabulary_is_the_spec_list():
    assert WORKLOADS == frozenset(
        {
            "chat",
            "agent_api",
            "builder",
            "extraction",
            "corporate_memory",
            "knowledge",
            "semantic_layer",
            "anonymization",
            "ocr",
            "vision",
            "auto_title",
            "readiness",
            "store_guardrails",
            "verification",
        }
    )
