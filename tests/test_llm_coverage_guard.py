"""Every ``messages.create(`` and every ``extract_json(`` in the modules the
design names (spec 3.3) sits inside labelled observability — a static scan,
so a new bypass fails CI.

Two independent predicates, because the two calls are labelled two
different ways: a ``messages.create(`` must sit inside ``trace_generation``
(which records AND traces the call), an ``extract_json(`` must sit inside
``llm_context`` (``extract_json``'s own provider implementation opens
``trace_generation`` internally — already covered by the first predicate in
``connectors/llm/anthropic_provider.py``/``connectors/llm/openai_compat.py``
— but that inner call has no way to see who's asking unless the CALLER sets
the ambient context first)."""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Every module spec 3.3's coverage table names, plus the two `connectors/llm`
# providers `anthropic_provider.py` doesn't cover on its own (`openai_compat`,
# `vertex_provider`) and `admin_usage.py` (the `admin_ask` workload's own
# call site).
COVERED_MODULES = [
    "connectors/sharepoint/facts_extraction.py",
    "src/ingest/scan_ocr.py",
    "src/ingest/vision.py",
    "src/anonymization_ner.py",
    "app/chat/auto_title.py",
    "app/chat/readiness.py",
    "app/api/entity_builder.py",
    "app/api/agent_builder.py",
    "app/api/mcp_builder.py",
    "app/api/package_builder.py",
    "app/api/semantic_model_builder.py",
    "services/corporate_memory/collector.py",
    "services/corporate_memory/tagger.py",
    "services/corporate_memory/contradiction.py",
    "src/knowledge_digests.py",
    "src/table_autodoc.py",
    "app/api/ontology.py",
    "app/services/memory_curator_profile.py",
    "services/session_processors/verification.py",
    "services/verification_detector/detector.py",
    "src/store_guardrails/craft_review.py",
    "src/store_guardrails/llm_review.py",
    "connectors/llm/anthropic_provider.py",
    "connectors/llm/openai_compat.py",
    "connectors/llm/vertex_provider.py",
    "app/api/admin_usage.py",
]


def _is_messages_create(call: ast.Call) -> bool:
    f = call.func
    return (
        isinstance(f, ast.Attribute)
        and f.attr == "create"
        and isinstance(f.value, ast.Attribute)
        and f.value.attr == "messages"
    )


def _is_extract_json(call: ast.Call) -> bool:
    f = call.func
    return isinstance(f, ast.Attribute) and f.attr == "extract_json"


def _with_calls(node: ast.With, name: str) -> bool:
    for item in node.items:
        ctx = item.context_expr
        if isinstance(ctx, ast.Call):
            fname = ctx.func.attr if isinstance(ctx.func, ast.Attribute) else getattr(ctx.func, "id", "")
            if fname == name:
                return True
    return False


def _uncovered_calls(path: Path, *, is_target: Callable[[ast.Call], bool], context_name: str) -> list[int]:
    """Line numbers of every ``is_target`` call in ``path`` that does not sit
    lexically inside a ``with <context_name>(...)`` block — a module with no
    matching call at all passes trivially (returns ``[]``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    withs = [n for n in ast.walk(tree) if isinstance(n, ast.With) and _with_calls(n, context_name)]
    missing = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and is_target(node)
            and not any(w.lineno <= node.lineno <= (w.end_lineno or w.lineno) for w in withs)
        ):
            missing.append(node.lineno)
    return missing


def _uncovered(path: Path) -> list[int]:
    return _uncovered_calls(path, is_target=_is_messages_create, context_name="trace_generation")


def _uncovered_extract_json(path: Path) -> list[int]:
    return _uncovered_calls(path, is_target=_is_extract_json, context_name="llm_context")


def _locations(module: str, lines: list[int]) -> str:
    return ", ".join(f"{module}:{lineno}" for lineno in lines)


@pytest.mark.parametrize("module", COVERED_MODULES)
def test_every_messages_create_is_traced(module):
    missing = _uncovered(REPO / module)
    assert not missing, f"messages.create( not inside trace_generation(...) at {_locations(module, missing)}"


@pytest.mark.parametrize("module", COVERED_MODULES)
def test_every_extract_json_call_sets_llm_context(module):
    missing = _uncovered_extract_json(REPO / module)
    assert not missing, f"extract_json( not inside a `with llm_context(...)` block at {_locations(module, missing)}"


def test_the_guard_sees_a_bypass(tmp_path):
    src = "def f(client):\n    return client.messages.create(model='m')\n"
    p = tmp_path / "m.py"
    p.write_text(src)
    assert _uncovered(p) == [2]
    covered = (
        "from src.observability import trace_generation\n"
        "def f(client):\n    with trace_generation(provider='anthropic', model='m') as cap:\n"
        "        return client.messages.create(model='m')\n"
    )
    p.write_text(covered)
    assert _uncovered(p) == []


def test_the_guard_sees_an_extract_json_bypass(tmp_path):
    """A module with no LLM call at all passes trivially; one that calls
    ``extract_json(`` with no ambient ``llm_context`` does not — the exact
    shape of the ``admin_usage.py`` gap this guard now closes."""
    p = tmp_path / "m.py"
    p.write_text("def f(x):\n    return 1\n")
    assert _uncovered_extract_json(p) == []

    bypass = "def f(extractor):\n    return extractor.extract_json(prompt='p', max_tokens=1, json_schema={}, schema_name='s')\n"
    p.write_text(bypass)
    assert _uncovered_extract_json(p) == [2]

    covered = (
        "from src.observability.llm_context import llm_context\n"
        "def f(extractor):\n"
        "    with llm_context(workload='admin_ask', purpose='telemetry_ask'):\n"
        "        return extractor.extract_json(prompt='p', max_tokens=1, json_schema={}, schema_name='s')\n"
    )
    p.write_text(covered)
    assert _uncovered_extract_json(p) == []
