"""Every ``messages.create(`` in the modules the design names (spec 3.3) sits
inside ``trace_generation`` — a static scan, so a new bypass fails CI."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

COVERED_MODULES = [
    "connectors/sharepoint/facts_extraction.py",
    "src/ingest/scan_ocr.py",
    "src/ingest/vision.py",
    "src/anonymization_ner.py",
    "app/chat/auto_title.py",
    "app/chat/readiness.py",
    "connectors/llm/anthropic_provider.py",
]


def _is_messages_create(call: ast.Call) -> bool:
    f = call.func
    return (
        isinstance(f, ast.Attribute)
        and f.attr == "create"
        and isinstance(f.value, ast.Attribute)
        and f.value.attr == "messages"
    )


def _with_calls_trace_generation(node: ast.With) -> bool:
    for item in node.items:
        ctx = item.context_expr
        if isinstance(ctx, ast.Call):
            name = ctx.func.attr if isinstance(ctx.func, ast.Attribute) else getattr(ctx.func, "id", "")
            if name == "trace_generation":
                return True
    return False


def _uncovered(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    withs = [n for n in ast.walk(tree) if isinstance(n, ast.With) and _with_calls_trace_generation(n)]
    missing = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_messages_create(node):
            if not any(w.lineno <= node.lineno <= (w.end_lineno or w.lineno) for w in withs):
                missing.append(node.lineno)
    return missing


@pytest.mark.parametrize("module", COVERED_MODULES)
def test_every_messages_create_is_traced(module):
    missing = _uncovered(REPO / module)
    assert not missing, f"{module}: messages.create( at line(s) {missing} is not inside trace_generation(...)"


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
